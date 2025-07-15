import asyncio
import json
import logging
from datetime import datetime
from sqlalchemy import and_
from contextlib import asynccontextmanager
from typing import Optional

from database import Notification, NotificationDelivery, User
from init import Session
from aiogram import Bot, types
from config import API_TOKEN

logger = logging.getLogger(__name__)


class SafeDict(dict):
    def __missing__(self, key):
        return '{' + key + '}'


class NotificationProcessor:
    def __init__(self, polling_interval: int = 10):
        """
        Сервис обработки и доставки уведомлений

        Args:
            polling_interval: интервал проверки в секундах (по умолчанию 30)
        """
        self.polling_interval = polling_interval
        self._running = False
        self._bot = None

    @staticmethod
    def _sequence_format(template: str, variables: dict, sequence_index: int = 0) -> str:
        """
        Formats string with variables, supporting both scalar and sequence values.
        For sequence values, uses value at sequence_index or last value if index out of range.
        """
        formatted_vars = {}

        for key, value in variables.items():
            if isinstance(value, (list, tuple)):
                try:
                    formatted_vars[key] = value[min(sequence_index, len(value) - 1)]
                except (IndexError, ValueError):
                    continue
            else:
                formatted_vars[key] = value

        return template.format_map(SafeDict(formatted_vars))

    @staticmethod
    def _create_keyboard(buttons_str: str, variables: dict = None) -> Optional[types.InlineKeyboardMarkup]:
        """
        Creates keyboard object from configuration string with variable support.
        Supports both the legacy format with '],[' and the new format with newlines.

        Format with '],[' (legacy):
        [button1:Text1; button2:Text2],[button3:Text3]

        Format with '||' (new recommended format):
        button1:Text1; button2:Text2
        ||
        button3:Text3

        Format with newlines (alternative):
        button1:Text1; button2:Text2
        button3:Text3
        """
        if not buttons_str or not buttons_str.strip():
            return None

        try:
            keyboard = types.InlineKeyboardMarkup()

            # Remove outer brackets if present
            cleaned_buttons = buttons_str.strip('[]')

            # Determine format and split rows accordingly
            if '||' in cleaned_buttons:
                # New format with explicit '||' delimiters
                rows = cleaned_buttons.split('||')
            elif '\n' in cleaned_buttons:
                # New format with newlines
                rows = cleaned_buttons.split('\n')
            else:
                # Legacy format with ],[ delimiters
                rows = cleaned_buttons.split('],[')

            sequence_index = 0

            for row in rows:
                # Skip empty rows
                if not row.strip():
                    continue

                # Clean row from any remaining brackets
                row = row.strip().strip('[]')

                button_row = []
                buttons = row.split(';')

                for button in buttons:
                    button = button.strip()
                    if not button or ':' not in button:
                        continue

                    callback, text = button.split(':', 1)
                    callback, text = callback.strip(), text.strip()

                    # Format both callback and text with variables if provided
                    if variables:
                        try:
                            if not callback.startswith('|url|'):
                                callback = NotificationProcessor._sequence_format(
                                    callback, variables, sequence_index
                                )
                            text = NotificationProcessor._sequence_format(
                                text, variables, sequence_index
                            )
                            sequence_index += 1
                        except Exception as e:
                            logger.error(f"Error formatting button: {e}")
                            continue

                    # Create the appropriate button type
                    if callback.startswith('|url|'):
                        url = 'https://' + callback[5:]
                        button_row.append(
                            types.InlineKeyboardButton(
                                text=text,
                                url=url
                            )
                        )
                    else:
                        button_row.append(
                            types.InlineKeyboardButton(
                                text=text,
                                callback_data=callback
                            )
                        )

                if button_row:
                    keyboard.row(*button_row)

            return keyboard if keyboard.inline_keyboard else None

        except Exception as e:
            logger.error(f"Error creating keyboard: {e}", exc_info=True)
            return None

    @asynccontextmanager
    async def get_bot(self):
        """
        Контекстный менеджер для безопасной работы с ботом
        """
        if self._bot is None:
            self._bot = Bot(token=API_TOKEN)
        try:
            yield self._bot
        finally:
            # Бот закрывается только при выходе из контекстного менеджера
            # и если это последний активный контекст
            if not self._running:
                await self._bot.session.close()
                self._bot = None

    async def process_filter(self, filter_json: str) -> list[int]:
        """Обрабатывает JSON с условиями фильтрации"""
        conditions = json.loads(filter_json)
        with Session() as session:
            query = session.query(User.userID)
            # TODO: Реализовать построение запросов на основе условий
            return []

    async def create_deliveries(self, notification: Notification) -> None:
        """Создает записи о доставке для уведомления"""
        with Session() as session:
            try:
                if notification.target_type == "user":
                    delivery = NotificationDelivery(
                        notificationID=notification.notificationID,
                        userID=int(notification.target_value)
                    )
                    session.add(delivery)

                elif notification.target_type == "all":
                    users = session.query(User.userID).all()
                    deliveries = [
                        NotificationDelivery(
                            notificationID=notification.notificationID,
                            userID=user.userID
                        ) for user in users
                    ]
                    session.bulk_save_objects(deliveries)

                elif notification.target_type == "filter":
                    user_ids = await self.process_filter(notification.target_value)
                    deliveries = [
                        NotificationDelivery(
                            notificationID=notification.notificationID,
                            userID=user_id
                        ) for user_id in user_ids
                    ]
                    session.bulk_save_objects(deliveries)

                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error creating deliveries: {e}")
                raise

    async def send_notification(self, delivery: NotificationDelivery) -> bool:
        """Отправляет одно уведомление"""
        try:
            with Session() as session:
                user = session.query(User).filter_by(userID=delivery.userID).first()
                if not user or not user.telegramID:
                    raise ValueError(f"User {delivery.userID} not found or has no telegram ID")

                notification = delivery.notification

                if notification.expiry_at and notification.expiry_at < datetime.utcnow():
                    delivery.status = "expired"
                    session.commit()
                    return False

                keyboard = None
                if notification.buttons:
                    # Создаем словарь с переменными для форматирования, если они есть
                    variables = {
                        'user_id': user.userID,
                        'telegram_id': user.telegramID,
                        # Добавьте другие переменные, которые могут использоваться в шаблонах кнопок
                    }
                    keyboard = NotificationProcessor._create_keyboard(notification.buttons, variables)

                async with self.get_bot() as bot:
                    message = await bot.send_message(
                        chat_id=user.telegramID,
                        text=notification.text,
                        parse_mode=notification.parse_mode,
                        reply_markup=keyboard,
                        disable_web_page_preview=notification.disable_web_page_preview,
                        disable_notification=notification.silent
                    )

                    if notification.auto_delete:
                        asyncio.create_task(self._schedule_deletion(
                            user.telegramID,
                            message.message_id,
                            notification.auto_delete
                        ))

                delivery.status = "sent"
                delivery.sent_at = datetime.utcnow()
                user.lastActive = datetime.utcnow()
                session.commit()

                return True

        except Exception as e:
            logger.error(f"Error sending notification {delivery.notificationID}: {e}")
            return False

    async def _schedule_deletion(self, chat_id: int, message_id: int, delay: int):
        """Отложенное удаление сообщения"""
        await asyncio.sleep(delay)
        try:
            async with self.get_bot() as bot:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"Error deleting message {message_id}: {e}")

    async def process_pending_deliveries(self) -> None:
        """Обрабатывает неотправленные уведомления"""
        with Session() as session:
            pending_deliveries = (
                session.query(NotificationDelivery)
                    .join(Notification)
                    .filter(and_(
                    NotificationDelivery.status == "pending",
                    NotificationDelivery.attempts < 3
                ))
                    .order_by(Notification.priority.desc())
                    .limit(50)
                    .all()
            )

            for delivery in pending_deliveries:
                success = await self.send_notification(delivery)

                delivery.attempts += 1
                if success:
                    delivery.status = "sent"
                    delivery.sent_at = datetime.utcnow()
                elif delivery.attempts >= 3:
                    delivery.status = "error"

                session.commit()

    async def process_new_notifications(self) -> None:
        """Создает записи о доставке для новых уведомлений"""
        with Session() as session:
            new_notifications = (
                session.query(Notification)
                    .outerjoin(NotificationDelivery)
                    .filter(NotificationDelivery.deliveryID == None)
                    .all()
            )

            for notification in new_notifications:
                try:
                    await self.create_deliveries(notification)
                except Exception as e:
                    logger.error(f"Error processing notification {notification.notificationID}: {e}")

    async def run(self) -> None:
        """Основной цикл обработки"""
        logger.info("Starting notification processor")
        self._running = True
        try:
            while self._running:
                try:
                    await self.process_new_notifications()
                    await self.process_pending_deliveries()
                except Exception as e:
                    logger.error(f"Error in notification processor: {e}")

                await asyncio.sleep(self.polling_interval)
        finally:
            self._running = False
            if self._bot:
                await self._bot.session.close()
                self._bot = None

    async def stop(self):
        """Безопасная остановка процессора"""
        self._running = False
        # Дождемся следующей итерации цикла для корректного завершения
        await asyncio.sleep(0)