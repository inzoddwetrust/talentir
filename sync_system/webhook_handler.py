"""
Webhook handler для экспорта данных из БД в Google Sheets
Принимает запросы от Google Apps Script и возвращает JSON с данными
Версия с улучшенной безопасностью и исправленной проверкой подписи
"""

import logging
import json
from aiohttp import web
from typing import Dict, Any, Optional, Set
import hashlib
import hmac
from datetime import datetime, timedelta
import ipaddress
from collections import defaultdict
import asyncio

from init import Session
from sync_system.sync_engine import UniversalSyncEngine
from sync_system.sync_config import SUPPORT_TABLES
from database import Notification
import config

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple rate limiter implementation"""

    def __init__(self, max_requests: int = 10, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window  # seconds
        self.requests = defaultdict(list)
        self._cleanup_task = None

    def is_allowed(self, client_id: str) -> bool:
        """Check if request is allowed for given client"""
        now = datetime.now()
        cutoff_time = now - timedelta(seconds=self.time_window)

        # Clean old requests
        self.requests[client_id] = [
            req_time for req_time in self.requests[client_id]
            if req_time > cutoff_time
        ]

        # Check limit
        if len(self.requests[client_id]) >= self.max_requests:
            return False

        # Add current request
        self.requests[client_id].append(now)
        return True

    async def cleanup_loop(self):
        """Periodic cleanup of old entries"""
        while True:
            await asyncio.sleep(300)  # Clean every 5 minutes
            now = datetime.now()
            cutoff_time = now - timedelta(seconds=self.time_window * 2)

            # Remove old client entries
            clients_to_remove = []
            for client_id, timestamps in self.requests.items():
                if all(ts < cutoff_time for ts in timestamps):
                    clients_to_remove.append(client_id)

            for client_id in clients_to_remove:
                del self.requests[client_id]

            if clients_to_remove:
                logger.debug(f"Cleaned up {len(clients_to_remove)} old rate limit entries")


class WebhookHandler:
    """Обработчик webhook запросов от Google Sheets с улучшенной безопасностью"""

    # Google Apps Script IP ranges (you should verify these)
    ALLOWED_IP_RANGES = [
        '34.64.0.0/10',      # Google Cloud
        '35.184.0.0/13',     # Google Cloud
        '35.192.0.0/11',     # Google Cloud
        '35.224.0.0/12',     # Google Cloud
        '35.240.0.0/13',     # Google Cloud
        '104.154.0.0/15',    # Google Cloud
        '104.196.0.0/14',    # Google Cloud
        '107.167.160.0/19',  # Google Cloud
        '107.178.192.0/18',  # Google Cloud
        '108.59.80.0/20',    # Google Cloud
        '108.170.192.0/18',  # Google Cloud
        '130.211.0.0/16',    # Google Cloud
        '146.148.0.0/17',    # Google Cloud
        '162.216.148.0/22',  # Google Cloud
        '162.222.176.0/21',  # Google Cloud
        '173.255.112.0/20',  # Google Cloud
        '192.158.28.0/22',   # Google Cloud
        '199.192.112.0/22',  # Google Cloud
        '199.223.232.0/21',  # Google Cloud
        '208.68.108.0/22',   # Google Cloud
        '23.236.48.0/20',    # Google Cloud
        '23.251.128.0/19',   # Google Cloud
    ]

    # Additional allowed IPs (for testing or specific services)
    ALLOWED_SPECIFIC_IPS: Set[str] = set()

    def __init__(self, secret_key: str = None):
        self.secret_key = secret_key or config.WEBHOOK_SECRET_KEY

        # Security check: secret key must be configured
        if not self.secret_key or self.secret_key == "error":
            logger.critical("WEBHOOK_SECRET_KEY is not properly configured!")
            raise ValueError("WEBHOOK_SECRET_KEY must be set in environment")

        # Initialize components
        self.app = web.Application()
        self.rate_limiter = RateLimiter(
            max_requests=config.WEBHOOK_RATE_LIMIT_REQUESTS if hasattr(config, 'WEBHOOK_RATE_LIMIT_REQUESTS') else 30,
            time_window=config.WEBHOOK_RATE_LIMIT_WINDOW if hasattr(config, 'WEBHOOK_RATE_LIMIT_WINDOW') else 60
        )

        # Health check token
        self.health_token = config.WEBHOOK_HEALTH_TOKEN if hasattr(config, 'WEBHOOK_HEALTH_TOKEN') else None

        # Metrics
        self.request_count = 0
        self.error_count = 0
        self.last_request_time = None

        # Load additional allowed IPs from config if available
        if hasattr(config, 'WEBHOOK_ALLOWED_IPS'):
            self.ALLOWED_SPECIFIC_IPS.update(config.WEBHOOK_ALLOWED_IPS)

        self.setup_routes()
        self.setup_middleware()

        # Start cleanup task
        asyncio.create_task(self.rate_limiter.cleanup_loop())

    def setup_routes(self):
        """Настройка маршрутов"""
        self.app.router.add_post('/sync/export', self.handle_export)
        self.app.router.add_get('/sync/health', self.handle_health)
        self.app.router.add_get('/sync/metrics', self.handle_metrics)

        # Catch-all for undefined routes
        self.app.router.add_route('*', '/{path:.*}', self.handle_not_found)

    def setup_middleware(self):
        """Setup middleware for request processing"""

        @web.middleware
        async def security_middleware(request, handler):
            """Security checks for all requests"""

            # Log request
            client_ip = self.get_client_ip(request)
            logger.info(f"Request from {client_ip}: {request.method} {request.path}")

            # Skip IP check for health endpoint if token is provided
            if request.path == '/sync/health':
                return await handler(request)

            # Check if IP is allowed
            if not self.is_ip_allowed(client_ip):
                logger.warning(f"Blocked request from unauthorized IP: {client_ip}")
                await self.notify_security_event(f"Blocked unauthorized IP: {client_ip}")
                return web.json_response(
                    {'error': 'Forbidden'},
                    status=403
                )

            # Check rate limit
            if not self.rate_limiter.is_allowed(client_ip):
                logger.warning(f"Rate limit exceeded for IP: {client_ip}")
                await self.notify_security_event(f"Rate limit exceeded: {client_ip}")
                return web.json_response(
                    {'error': 'Too Many Requests'},
                    status=429
                )

            # Process request
            try:
                response = await handler(request)
                return response
            except Exception as e:
                logger.error(f"Error processing request: {e}", exc_info=True)
                self.error_count += 1
                return web.json_response(
                    {'error': 'Internal Server Error'},
                    status=500
                )

        self.app.middlewares.append(security_middleware)

    def get_client_ip(self, request: web.Request) -> str:
        """Get real client IP from request"""
        # Добавьте отладку
        logger.info(
            f"Headers: X-Forwarded-For={request.headers.get('X-Forwarded-For')}, X-Real-IP={request.headers.get('X-Real-IP')}")

        # Check for proxy headers
        if 'X-Forwarded-For' in request.headers:
            # Take the first IP from the chain
            ip = request.headers['X-Forwarded-For'].split(',')[0].strip()
            logger.info(f"Using X-Forwarded-For: {ip}")
            return ip
        elif 'X-Real-IP' in request.headers:
            ip = request.headers['X-Real-IP']
            logger.info(f"Using X-Real-IP: {ip}")
            return ip
        else:
            # Fallback to remote address
            peername = request.transport.get_extra_info('peername')
            if peername:
                ip = peername[0]
                logger.info(f"Using peername: {ip}")
                return ip
            return 'unknown'

    def is_ip_allowed(self, client_ip: str) -> bool:
        """Check if client IP is allowed"""

        # Allow localhost for testing
        if client_ip in ['127.0.0.1', '::1', 'localhost']:
            return True

        # Check specific allowed IPs
        if client_ip in self.ALLOWED_SPECIFIC_IPS:
            return True

        # Check IP ranges
        try:
            client_ip_obj = ipaddress.ip_address(client_ip)
            for ip_range in self.ALLOWED_IP_RANGES:
                if client_ip_obj in ipaddress.ip_network(ip_range):
                    return True
        except ValueError:
            logger.error(f"Invalid IP address format: {client_ip}")
            return False

        return False

    def verify_signature(self, data_dict: Dict, signature: str) -> bool:
        """
        Проверка подписи запроса
        ИСПРАВЛЕНО: Теперь проверяет подпись от данных БЕЗ самой подписи
        """
        if not signature:
            logger.warning("No signature provided in request")
            return False

        # Создаем копию данных без подписи
        data_for_verification = data_dict.copy()
        data_for_verification.pop('signature', None)

        # Сортируем ключи для консистентности
        payload_json = json.dumps(data_for_verification, sort_keys=True, separators=(',', ':'))

        # Генерируем ожидаемую подпись
        expected = hmac.new(
            self.secret_key.encode('utf-8'),
            payload_json.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        # Use constant-time comparison
        is_valid = hmac.compare_digest(expected, signature)

        if not is_valid:
            logger.warning(f"Invalid signature. Expected: {expected[:10]}..., Got: {signature[:10]}...")
            # Для отладки (потом удалить)
            logger.debug(f"Payload for verification: {payload_json[:100]}...")

        return is_valid

    async def notify_security_event(self, message: str):
        """Send notification about security events to admins"""
        try:
            with Session() as session:
                # Create notification for admins
                notification = Notification(
                    source="webhook_security",
                    text=f"🔒 Security Alert\n\n{message}\n\nTime: {datetime.now().isoformat()}",
                    target_type="admins",
                    target_value="all",
                    priority=3,
                    category="security",
                    importance="high",
                    parse_mode="HTML"
                )
                session.add(notification)
                session.commit()
        except Exception as e:
            logger.error(f"Failed to send security notification: {e}")

    async def handle_not_found(self, request: web.Request) -> web.Response:
        """Handle undefined routes"""
        client_ip = self.get_client_ip(request)
        logger.warning(f"404 Not Found: {request.path} from {client_ip}")

        # Don't reveal internal structure
        return web.Response(
            text='Not Found',
            status=404
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """Проверка работоспособности webhook (защищенная)"""

        # Check health token if configured
        if self.health_token:
            provided_token = request.headers.get('X-Health-Token')
            if provided_token != self.health_token:
                logger.warning(f"Invalid health check token from {self.get_client_ip(request)}")
                return web.json_response(
                    {'error': 'Unauthorized'},
                    status=401
                )

        return web.json_response({
            'status': 'ok',
            'timestamp': datetime.now().isoformat(),
            'service': 'talentir-sync-webhook',
            'version': '2.1.0'  # Updated version with fixed signature
        })

    async def handle_metrics(self, request: web.Request) -> web.Response:
        """Get service metrics (protected endpoint)"""

        # Require health token for metrics access
        if not self.health_token or request.headers.get('X-Health-Token') != self.health_token:
            return web.json_response({'error': 'Unauthorized'}, status=401)

        return web.json_response({
            'requests_total': self.request_count,
            'errors_total': self.error_count,
            'last_request': self.last_request_time.isoformat() if self.last_request_time else None,
            'uptime_seconds': (datetime.now() - self.start_time).total_seconds() if hasattr(self, 'start_time') else 0
        })

    async def handle_export(self, request: web.Request) -> web.Response:
        """
        Обработка запроса на экспорт данных с улучшенной безопасностью

        Ожидаемый запрос:
        {
            "table": "Users",
            "signature": "sha256_hash",
            "timestamp": "2025-01-01T00:00:00Z",  // для защиты от replay атак
            "nonce": "random_string",  // опционально
            "filters": {}  // опционально
        }
        """

        # Update metrics
        self.request_count += 1
        self.last_request_time = datetime.now()
        client_ip = self.get_client_ip(request)

        try:
            # Read request body
            body = await request.read()

            # Size limit check (prevent large payload attacks)
            if len(body) > 1024 * 100:  # 100KB limit
                logger.warning(f"Request body too large from {client_ip}: {len(body)} bytes")
                return web.json_response(
                    {'error': 'Request too large'},
                    status=413
                )

            # Parse JSON
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON from {client_ip}: {e}")
                return web.json_response(
                    {'error': 'Invalid JSON'},
                    status=400
                )

            # Check timestamp (prevent replay attacks)
            if 'timestamp' in data:
                try:
                    request_time = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
                    time_diff = abs((datetime.now() - request_time).total_seconds())
                    if time_diff > 300:  # 5 minutes tolerance
                        logger.warning(f"Request timestamp too old from {client_ip}: {time_diff} seconds")
                        return web.json_response(
                            {'error': 'Request expired'},
                            status=400
                        )
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid timestamp format from {client_ip}: {e}")
                    return web.json_response(
                        {'error': 'Invalid timestamp'},
                        status=400
                    )

            # ИСПРАВЛЕНО: Проверяем подпись от данных БЕЗ самой подписи
            signature = data.get('signature', '')
            if not self.verify_signature(data, signature):
                logger.warning(f"Invalid signature from {client_ip} for table {data.get('table', 'unknown')}")
                await self.notify_security_event(
                    f"Invalid webhook signature from {client_ip}\n"
                    f"Table: {data.get('table', 'unknown')}\n"
                    f"Timestamp: {data.get('timestamp', 'not provided')}"
                )
                return web.json_response(
                    {'error': 'Invalid signature'},
                    status=401
                )

            # Validate table name
            table_name = data.get('table')
            if not table_name:
                return web.json_response(
                    {'error': 'Table name required'},
                    status=400
                )

            # Sanitize table name (prevent injection)
            if not table_name.replace('_', '').isalnum():
                logger.warning(f"Invalid table name format from {client_ip}: {table_name}")
                return web.json_response(
                    {'error': 'Invalid table name format'},
                    status=400
                )

            # Check if table is allowed for export
            if table_name not in SUPPORT_TABLES:
                logger.warning(f"Unauthorized table access attempt from {client_ip}: {table_name}")
                await self.notify_security_event(
                    f"Unauthorized table access attempt from {client_ip}\n"
                    f"Table: {table_name}"
                )
                return web.json_response(
                    {'error': f'Table {table_name} not allowed for export'},
                    status=403
                )

            # Export data
            logger.info(f"Export request for table {table_name} from {client_ip}")

            with Session() as session:
                engine = UniversalSyncEngine(table_name)
                result = engine.export_to_json(session)

            if result['success']:
                logger.info(f"Successfully exported {result['count']} records from {table_name} to {client_ip}")
                return web.json_response(result)
            else:
                logger.error(f"Export failed for {table_name}: {result.get('error')}")
                self.error_count += 1
                return web.json_response(
                    {'error': result.get('error', 'Export failed')},
                    status=500
                )

        except Exception as e:
            logger.error(f"Webhook error: {e}", exc_info=True)
            self.error_count += 1
            await self.notify_security_event(
                f"Webhook error from {client_ip}\n"
                f"Error: {str(e)}"
            )
            return web.json_response(
                {'error': 'Internal server error'},
                status=500
            )

    async def start(self, host: str = '127.0.0.1', port: int = 8080):
        """
        Запуск webhook сервера

        По умолчанию слушает только localhost для безопасности.
        Используйте reverse proxy (nginx) для внешнего доступа.
        """
        self.start_time = datetime.now()

        # Override host from config if available
        if hasattr(config, 'WEBHOOK_HOST'):
            host = config.WEBHOOK_HOST

        # Security warning for public interface
        if host == '0.0.0.0':
            logger.warning("⚠️ Webhook server is listening on all interfaces! Ensure firewall is configured.")

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        logger.info(f"🔒 Secure webhook server started on {host}:{port}")
        logger.info(f"Secret key configured: {'✅' if self.secret_key else '❌'}")
        logger.info(f"Health token configured: {'✅' if self.health_token else '❌'}")
        logger.info(f"Rate limiting: {self.rate_limiter.max_requests} requests per {self.rate_limiter.time_window} seconds")

        return runner


# Функция для запуска в main.py
async def start_webhook_server():
    """Запускает webhook сервер для синхронизации с улучшенной безопасностью"""
    try:
        handler = WebhookHandler()
        runner = await handler.start(
            host=config.WEBHOOK_HOST if hasattr(config, 'WEBHOOK_HOST') else '127.0.0.1',
            port=config.WEBHOOK_PORT if hasattr(config, 'WEBHOOK_PORT') else 8080
        )
        return runner
    except ValueError as e:
        logger.critical(f"Failed to start webhook server: {e}")
        raise
    except Exception as e:
        logger.critical(f"Unexpected error starting webhook server: {e}", exc_info=True)
        raise