import logging
import aiohttp
from typing import Dict, Any, Optional

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
            logger.info(f"Attempting to send email to {to} with subject: {subject}")

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


class EmailManager:
    """Менеджер для работы с email через Postmark"""

    def __init__(self):
        if not hasattr(config, 'POSTMARK_API_TOKEN') or not config.POSTMARK_API_TOKEN:
            logger.warning("POSTMARK_API_TOKEN not configured")
            self.provider = None
        else:
            self.provider = PostmarkProvider(config.POSTMARK_API_TOKEN)
            logger.info("EmailManager initialized with Postmark provider")

    async def send_verification_email(self, user, verification_link: str) -> bool:
        """
        Отправляет письмо верификации пользователю

        Args:
            user: Объект пользователя из БД
            verification_link: Ссылка для верификации

        Returns:
            bool: True если письмо отправлено успешно
        """
        if not self.provider:
            logger.error("Email provider not configured")
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

            # Отправляем email только с HTML версией
            success = await self.provider.send_email(
                to=user.email,
                subject=subject_text,
                html_body=body_html,
                text_body=None  # Никаких текстовых версий!
            )

            if success:
                logger.info(f"Verification email sent successfully to {user.email}")
            else:
                logger.error(f"Failed to send verification email to {user.email}")

            return success

        except Exception as e:
            logger.error(f"Error in send_verification_email: {e}")
            logger.exception("Full traceback:")
            return False

    async def send_notification_email(self, to: str, subject: str, body: str) -> bool:
        """
        Отправляет произвольное уведомление

        Args:
            to: Email получателя
            subject: Тема письма
            body: Тело письма (HTML)

        Returns:
            bool: True если письмо отправлено успешно
        """
        if not self.provider:
            logger.error("Email provider not configured")
            return False

        return await self.provider.send_email(to, subject, body)

    async def test_connection(self) -> bool:
        """
        Тестирует подключение к Postmark API

        Returns:
            bool: True если подключение успешно
        """
        if not self.provider:
            logger.error("Email provider not configured")
            return False

        try:
            logger.info("Testing Postmark connection...")

            # Используем endpoint для получения информации о сервере
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{self.provider.base_url}/server",
                        headers=self.provider.headers
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


# Singleton instance
email_manager = EmailManager()