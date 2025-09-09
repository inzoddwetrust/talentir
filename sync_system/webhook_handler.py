"""
Webhook handler для экспорта данных из БД в Google Sheets
Принимает запросы от Google Apps Script и возвращает JSON с данными
"""

import logging
import json
from aiohttp import web
from typing import Dict, Any
import hashlib
import hmac
from datetime import datetime

from init import Session
from sync_system.sync_engine import UniversalSyncEngine
from sync_system.sync_config import SUPPORT_TABLES
import config

logger = logging.getLogger(__name__)


class WebhookHandler:
    """Обработчик webhook запросов от Google Sheets"""

    def __init__(self, secret_key: str = None):
        self.secret_key = secret_key or config.WEBHOOK_SECRET_KEY
        self.app = web.Application()
        self.setup_routes()

    def setup_routes(self):
        """Настройка маршрутов"""
        self.app.router.add_post('/sync/export', self.handle_export)
        self.app.router.add_get('/sync/health', self.handle_health)

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """Проверка подписи запроса"""
        if not self.secret_key:
            return True  # Если ключ не установлен, пропускаем проверку

        expected = hmac.new(
            self.secret_key.encode(),
            payload,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    async def handle_health(self, request: web.Request) -> web.Response:
        """Проверка работоспособности webhook"""
        return web.json_response({
            'status': 'ok',
            'timestamp': datetime.now().isoformat(),
            'service': 'talentir-sync-webhook'
        })

    async def handle_export(self, request: web.Request) -> web.Response:
        """
        Обработка запроса на экспорт данных

        Ожидаемый запрос:
        {
            "table": "Users",  // или другая таблица
            "signature": "sha256_hash",  // подпись для авторизации
            "filters": {}  // опционально: фильтры (пока не реализовано)
        }
        """
        try:
            # Читаем тело запроса
            body = await request.read()

            # Парсим JSON
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return web.json_response(
                    {'error': 'Invalid JSON'},
                    status=400
                )

            # Проверяем подпись
            signature = data.get('signature', '')
            if not self.verify_signature(body, signature):
                logger.warning(f"Invalid signature from {request.remote}")
                return web.json_response(
                    {'error': 'Invalid signature'},
                    status=401
                )

            # Получаем имя таблицы
            table_name = data.get('table')
            if not table_name:
                return web.json_response(
                    {'error': 'Table name required'},
                    status=400
                )

            # Проверяем, что таблица разрешена для экспорта
            if table_name not in SUPPORT_TABLES:
                return web.json_response(
                    {'error': f'Table {table_name} not allowed for export'},
                    status=403
                )

            # Экспортируем данные
            logger.info(f"Export request for table {table_name} from {request.remote}")

            with Session() as session:
                engine = UniversalSyncEngine(table_name)
                result = engine.export_to_json(session)

            if result['success']:
                logger.info(f"Exported {result['count']} records from {table_name}")
                return web.json_response(result)
            else:
                logger.error(f"Export failed for {table_name}: {result.get('error')}")
                return web.json_response(
                    {'error': result.get('error', 'Export failed')},
                    status=500
                )

        except Exception as e:
            logger.error(f"Webhook error: {e}", exc_info=True)
            return web.json_response(
                {'error': 'Internal server error'},
                status=500
            )

    async def start(self, host: str = '0.0.0.0', port: int = 8080):
        """Запуск webhook сервера"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Webhook server started on {host}:{port}")
        return runner


# Функция для запуска в main.py
async def start_webhook_server():
    """Запускает webhook сервер для синхронизации"""
    handler = WebhookHandler()
    runner = await handler.start(port=config.WEBHOOK_PORT)
    return runner