import logging
import aiohttp
import aiosmtplib
from typing import Dict, Any, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import config
from templates import MessageTemplates

logger = logging.getLogger(__name__)


class PostmarkProvider:
    """Postmark API провайдер для отправки email"""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base_url = "https://api.postmarkapp.com"
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": api_token
        }

    async def send_email(self, to: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
        """
        Отправляет email через Postmark API

        Args:
            to: Email получателя
            subject: Тема письма
            html_body: HTML версия письма
            text_body: Текстовая версия письма (опционально)

        Returns:
            bool: True если письмо отправлено успешно
        """
        try:
            logger.info(f"Attempting to send email via Postmark to {to} with subject: {subject}")

            # Подготавливаем данные для отправки
            data = {
                "From": f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>",
                "To": to,
                "Subject": subject,
                "HtmlBody": html_body,
                "MessageStream": "outbound"  # Используем транзакционный поток
            }

            # Добавляем текстовую версию, если предоставлена
            if text_body:
                data["TextBody"] = text_body

            # Отправляем запрос к API
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{self.base_url}/email",
                        headers=self.headers,
                        json=data
                ) as response:
                    response_data = await response.json()

                    if response.status == 200:
                        logger.info(
                            f"Email sent successfully via Postmark to {to}. MessageID: {response_data.get('MessageID')}")
                        return True
                    else:
                        error_message = response_data.get('Message', 'Unknown error')
                        error_code = response_data.get('ErrorCode', 'Unknown')
                        logger.error(f"Postmark API error ({error_code}): {error_message}")
                        logger.error(f"Full response: {response_data}")

                        # Логируем дополнительную информацию для отладки
                        if error_code == 406:
                            logger.error("Inactive recipient - email address may be on suppression list")
                        elif error_code == 300:
                            logger.error("Invalid email request - check email format and content")
                        elif error_code == 10:
                            logger.error("Bad or missing API token")
                        elif error_code == 422:
                            logger.error("Unprocessable Entity - check From address is verified")

                        return False

        except aiohttp.ClientError as e:
            logger.error(f"Network error while sending email via Postmark: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error while sending email via Postmark: {e}")
            logger.exception("Full traceback:")
            return False

    async def send_template_email(self, to: str, template_alias: str, template_model: Dict[str, Any]) -> bool:
        """
        Отправляет email используя шаблон Postmark

        Args:
            to: Email получателя
            template_alias: Алиас шаблона в Postmark
            template_model: Данные для подстановки в шаблон

        Returns:
            bool: True если письмо отправлено успешно
        """
        try:
            data = {
                "From": f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>",
                "To": to,
                "TemplateAlias": template_alias,
                "TemplateModel": template_model,
                "MessageStream": "outbound"
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{self.base_url}/email/withTemplate",
                        headers=self.headers,
                        json=data
                ) as response:
                    response_data = await response.json()

                    if response.status == 200:
                        logger.info(f"Template email sent successfully via Postmark to {to}")
                        return True
                    else:
                        logger.error(f"Postmark template API error: {response_data}")
                        return False

        except Exception as e:
            logger.error(f"Error sending template email via Postmark: {e}")
            return False

    async def test_connection(self) -> bool:
        """
        Тестирует подключение к Postmark API

        Returns:
            bool: True если подключение успешно
        """
        try:
            logger.info("Testing Postmark connection...")

            # Используем endpoint для получения информации о сервере
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{self.base_url}/server",
                        headers=self.headers
                ) as response:
                    if response.status == 200:
                        server_info = await response.json()
                        logger.info(f"Successfully connected to Postmark server: {server_info.get('Name')}")
                        return True
                    else:
                        response_data = await response.json()
                        logger.error(f"Failed to connect to Postmark: {response.status}")
                        logger.error(f"Response: {response_data}")
                        return False

        except Exception as e:
            logger.error(f"Error testing Postmark connection: {e}")
            logger.exception("Full traceback:")
            return False


class SMTPProvider:
    """SMTP провайдер для собственного почтового сервера"""

    def __init__(self, smtp_host: str, smtp_port: int, username: str, password: str):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password

    async def send_email(self, to: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
        """
        Отправляет email через SMTP сервер

        Args:
            to: Email получателя
            subject: Тема письма
            html_body: HTML версия письма
            text_body: Текстовая версия письма (опционально)

        Returns:
            bool: True если письмо отправлено успешно
        """
        try:
            logger.info(f"Attempting to send email via SMTP to {to} with subject: {subject}")

            # Создаем сообщение
            if text_body:
                # Если есть и HTML и текст - создаем multipart
                message = MIMEMultipart('alternative')
                message['From'] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
                message['To'] = to
                message['Subject'] = subject

                # Добавляем текстовую и HTML части
                text_part = MIMEText(text_body, 'plain', 'utf-8')
                html_part = MIMEText(html_body, 'html', 'utf-8')

                message.attach(text_part)
                message.attach(html_part)
            else:
                # Только HTML версия
                message = MIMEText(html_body, 'html', 'utf-8')
                message['From'] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
                message['To'] = to
                message['Subject'] = subject

            # Отправляем через SMTP
            await aiosmtplib.send(
                message,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.username,
                password=self.password,
                start_tls=True,
                use_tls=False,  # Добавляем эту строку
                timeout=30
            )

            logger.info(f"Email sent successfully via SMTP to {to}")
            return True

        except aiosmtplib.SMTPException as e:
            logger.error(f"SMTP error while sending email to {to}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error while sending email via SMTP to {to}: {e}")
            logger.exception("Full traceback:")
            return False

    async def test_connection(self) -> bool:
        """
        Тестирует подключение к SMTP серверу

        Returns:
            bool: True если подключение успешно
        """
        try:
            logger.info(f"Testing SMTP connection to {self.smtp_host}:{self.smtp_port}")

            # Пробуем подключиться и аутентифицироваться
            smtp = aiosmtplib.SMTP(hostname=self.smtp_host, port=self.smtp_port)
            await smtp.connect()
            await smtp.starttls()
            await smtp.login(self.username, self.password)
            await smtp.quit()

            logger.info("SMTP connection test successful")
            return True

        except Exception as e:
            logger.error(f"SMTP connection test failed: {e}")
            return False


class EmailManager:
    """Менеджер для работы с email через несколько провайдеров"""

    def __init__(self):
        self.providers = []

        # Постмарк как основной провайдер
        if hasattr(config, 'POSTMARK_API_TOKEN') and config.POSTMARK_API_TOKEN:
            self.providers.append(PostmarkProvider(config.POSTMARK_API_TOKEN))
            logger.info("EmailManager: Added Postmark provider")

        # SMTP как фоллбек провайдер
        if all([
            hasattr(config, 'SMTP_HOST') and config.SMTP_HOST,
            hasattr(config, 'SMTP_USER') and config.SMTP_USER,
            hasattr(config, 'SMTP_PASSWORD') and config.SMTP_PASSWORD
        ]):
            smtp_port = getattr(config, 'SMTP_PORT', 587)
            self.providers.append(SMTPProvider(
                config.SMTP_HOST,
                smtp_port,
                config.SMTP_USER,
                config.SMTP_PASSWORD
            ))
            logger.info(f"EmailManager: Added SMTP provider ({config.SMTP_HOST}:{smtp_port})")

        if not self.providers:
            logger.warning("EmailManager: No email providers configured")

    async def send_verification_email(self, user, verification_link: str) -> bool:
        """
        Отправляет письмо верификации пользователю через доступные провайдеры

        Args:
            user: Объект пользователя из БД
            verification_link: Ссылка для верификации

        Returns:
            bool: True если письмо отправлено успешно
        """
        if not self.providers:
            logger.error("No email providers configured")
            return False

        try:
            logger.info(f"Preparing verification email for user {user.userID} ({user.email})")

            # Получаем шаблоны из Google Sheets
            subject_text, _ = await MessageTemplates.get_raw_template(
                'email_verification_subject',
                {
                    'firstname': user.firstname,
                    'projectName': 'Talentir'
                },
                lang=user.lang
            )

            body_html, _ = await MessageTemplates.get_raw_template(
                'email_verification_body',
                {
                    'firstname': user.firstname,
                    'verification_link': verification_link,
                    'email': user.email
                },
                lang=user.lang
            )

            logger.info(f"Templates loaded. Subject: {subject_text[:50]}...")

            # Пробуем отправить через каждый провайдер по очереди
            for i, provider in enumerate(self.providers):
                try:
                    provider_name = provider.__class__.__name__
                    logger.info(f"Trying provider {i + 1}/{len(self.providers)}: {provider_name}")

                    success = await provider.send_email(
                        to=user.email,
                        subject=subject_text,
                        html_body=body_html,
                        text_body=None
                    )

                    if success:
                        logger.info(f"✅ Verification email sent successfully via {provider_name} to {user.email}")
                        return True
                    else:
                        logger.warning(f"❌ Provider {provider_name} failed to send email")

                except Exception as e:
                    provider_name = provider.__class__.__name__
                    logger.error(f"❌ Provider {provider_name} error: {e}")
                    continue

            # Если все провайдеры не сработали
            logger.error(f"❌ All email providers failed to send verification email to {user.email}")
            return False

        except Exception as e:
            logger.error(f"Error in send_verification_email: {e}")
            logger.exception("Full traceback:")
            return False

    async def send_notification_email(self, to: str, subject: str, body: str) -> bool:
        """
        Отправляет произвольное уведомление через доступные провайдеры

        Args:
            to: Email получателя
            subject: Тема письма
            body: Тело письма (HTML)

        Returns:
            bool: True если письмо отправлено успешно
        """
        if not self.providers:
            logger.error("No email providers configured")
            return False

        # Пробуем отправить через каждый провайдер по очереди
        for i, provider in enumerate(self.providers):
            try:
                provider_name = provider.__class__.__name__
                logger.info(f"Trying provider {i + 1}/{len(self.providers)}: {provider_name}")

                success = await provider.send_email(to, subject, body)

                if success:
                    logger.info(f"✅ Notification email sent successfully via {provider_name} to {to}")
                    return True
                else:
                    logger.warning(f"❌ Provider {provider_name} failed to send notification")

            except Exception as e:
                provider_name = provider.__class__.__name__
                logger.error(f"❌ Provider {provider_name} error: {e}")
                continue

        # Если все провайдеры не сработали
        logger.error(f"❌ All email providers failed to send notification to {to}")
        return False

    async def test_connection(self) -> bool:
        """
        Тестирует подключение ко всем провайдерам

        Returns:
            bool: True если хотя бы один провайдер работает
        """
        if not self.providers:
            logger.error("No email providers configured")
            return False

        any_working = False

        for provider in self.providers:
            provider_name = provider.__class__.__name__

            try:
                if hasattr(provider, 'test_connection'):
                    success = await provider.test_connection()
                else:
                    # Для Postmark провайдера делаем простой запрос к API
                    if provider_name == 'PostmarkProvider':
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                    f"{provider.base_url}/server",
                                    headers=provider.headers
                            ) as response:
                                success = response.status == 200
                    else:
                        success = False

                if success:
                    logger.info(f"✅ {provider_name} connection test: SUCCESS")
                    any_working = True
                else:
                    logger.warning(f"❌ {provider_name} connection test: FAILED")

            except Exception as e:
                logger.error(f"❌ {provider_name} connection test error: {e}")

        return any_working

    async def get_providers_status(self) -> Dict[str, bool]:
        """
        Возвращает статус всех провайдеров

        Returns:
            Dict[str, bool]: Словарь провайдер -> статус
        """
        status = {}

        for provider in self.providers:
            provider_name = provider.__class__.__name__

            try:
                if hasattr(provider, 'test_connection'):
                    status[provider_name] = await provider.test_connection()
                else:
                    # Для Postmark провайдера
                    if provider_name == 'PostmarkProvider':
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                    f"{provider.base_url}/server",
                                    headers=provider.headers
                            ) as response:
                                status[provider_name] = response.status == 200
                    else:
                        status[provider_name] = False

            except Exception:
                status[provider_name] = False

        return status


# Singleton instance
email_manager = EmailManager()