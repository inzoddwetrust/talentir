import asyncio
import logging
from datetime import datetime, timedelta

from database import Payment, Notification
from init import Session
from templates import MessageTemplates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class InvoiceCleaner:
    def __init__(self, bot_username: str, check_interval: int = 900):
        self.bot_username = bot_username
        self.check_interval = check_interval

    def format_remaining_time(self, remaining: timedelta) -> str:
        return str(int(remaining.total_seconds() / 60))

    async def expire_invoice(self, session, invoice: Payment):
        try:
            invoice.status = "expired"

            # Получаем текст и кнопки из шаблона
            text, buttons = await MessageTemplates.get_raw_template(
                'invoice_expired',  # Новый шаблон в Google Sheets
                {
                    'amount': invoice.amount,
                    'method': invoice.method
                }
            )

            notification = Notification(
                source="invoice_cleaner",
                text=text,
                target_type="user",
                target_value=str(invoice.userID),
                priority=2,
                category="payment",
                importance="high",
                parse_mode="HTML",
                buttons=buttons
            )

            session.add(notification)
            session.commit()
            logger.info(f"Invoice {invoice.paymentID} marked as expired")

        except Exception as e:
            logger.error(f"Error expiring invoice {invoice.paymentID}: {e}")
            session.rollback()

    async def send_warning(self, session, invoice: Payment, remaining: timedelta):
        try:
            text, buttons = await MessageTemplates.get_raw_template(
                'invoice_warning',  # Новый шаблон в Google Sheets
                {
                    'amount': invoice.amount,
                    'method': invoice.method,
                    'payment_id': invoice.paymentID,
                    'bot_username': self.bot_username,
                    'remaining_time': self.format_remaining_time(remaining)
                }
            )

            notification = Notification(
                source="invoice_cleaner",
                text=text,
                target_type="user",
                target_value=str(invoice.userID),
                priority=2,
                category="payment",
                importance="high",
                parse_mode="HTML",
                buttons=buttons
            )

            session.add(notification)
            session.commit()
            logger.info(f"Warning notification sent for invoice {invoice.paymentID}")

        except Exception as e:
            logger.error(f"Error sending warning for invoice {invoice.paymentID}: {e}")
            session.rollback()

    async def process_pending_invoices(self):
        """
        Обработка просроченных инвойсов
        """
        with Session() as session:
            try:
                # Получаем все ожидающие оплаты инвойсы старше 1 часа
                one_hour_ago = datetime.utcnow() - timedelta(hours=1)
                pending_invoices = (
                    session.query(Payment)
                        .filter(
                        Payment.status == "pending",
                        Payment.createdAt <= one_hour_ago
                    )
                        .all()
                )

                for invoice in pending_invoices:
                    age = datetime.utcnow() - invoice.createdAt

                    # Если прошло больше 2 часов - помечаем как просроченный
                    if age > timedelta(hours=2):
                        await self.expire_invoice(session, invoice)
                    # Если прошло больше 1 часа - отправляем предупреждение
                    elif age > timedelta(hours=1):
                        remaining = timedelta(hours=2) - age
                        await self.send_warning(session, invoice, remaining)

            except Exception as e:
                logger.error(f"Error processing pending invoices: {e}")
                session.rollback()

    async def run(self):
        """
        Запускает процесс проверки инвойсов
        """
        logger.info("Invoice cleaner started")
        self._running = True

        while self._running:
            try:
                await self.process_pending_invoices()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Error in invoice cleaner main loop: {e}")
                await asyncio.sleep(self.check_interval)

    async def stop(self):
        """
        Останавливает процесс проверки инвойсов
        """
        self._running = False
        logger.info("Invoice cleaner stopped")
