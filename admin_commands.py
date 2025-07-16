import logging
from aiogram.dispatcher.filters import Filter
from aiogram.dispatcher import FSMContext
from aiogram import Dispatcher, types
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware
from bookstack_integration import clear_template_cache

import config
from imports import (
    ProjectImporter, UserImporter, OptionImporter,
    PaymentImporter, PurchaseImporter, BonusImporter, ConfigImporter, import_all
)
from templates import MessageTemplates
from google_services import get_google_services
from sqlalchemy import func
from database import Payment, Notification, User
from init import Session

logger = logging.getLogger(__name__)


class AdminFilter(Filter):
    key = 'is_admin'  # Обязательно нужен key для фильтра

    async def check(self, message: types.Message) -> bool:
        return message.from_user.id in config.ADMINS


class AdminCommandsMiddleware(BaseMiddleware):
    def __init__(self, admin_commands_instance):
        super().__init__()
        self.admin_commands_instance = admin_commands_instance

    async def on_process_message(self, message: types.Message, data: dict):
        if message.from_user.id in config.ADMINS and message.text and message.text.startswith('&'):

            state = data['state']
            current_state = await state.get_state()

            if current_state:
                logger.info(f"Сброшено состояние {current_state} для администратора")
                await state.finish()

            await self.admin_commands_instance.handle_admin_command(message, state)

            raise CancelHandler()


class AdminCommands:
    def __init__(self, dp):
        self.dp = dp
        self.register_handlers()

    def register_handlers(self):
        """Регистрация всех обработчиков админских команд"""
        # Добавляем проверку на наличие текста в сообщении
        self.dp.register_message_handler(
            self.handle_admin_command,
            AdminFilter(),
            lambda msg: msg.text and msg.text.startswith('&'),
            state='*'
        )

    async def _import_sheet(self, message: types.Message, importer_class, sheet_name: str):
        """Общий метод для импорта данных"""
        reply = None
        try:
            reply = await message.reply(f"Начинаю импорт {sheet_name}...")

            sheets_client, _ = get_google_services()
            sheet = sheets_client.open_by_key(config.GOOGLE_SHEET_ID).worksheet(sheet_name)

            importer = importer_class()
            stats = await importer.import_sheet(sheet)

            report = (
                f"✅ Импорт {sheet_name} завершен:\n"
                f"Всего строк: {stats.total}\n"
                f"Обновлено: {stats.updated}\n"
                f"Добавлено: {stats.added}\n"
                f"Пропущено: {stats.skipped}\n"
                f"Ошибок: {stats.errors}"
            )

            if stats.error_rows:
                report += "\n\nОшибки:\n" + "\n".join(
                    f"Строка {row}: {error}" for row, error in stats.error_rows
                )

            await reply.edit_text(report)

        except Exception as e:
            error_msg = f"❌ Ошибка при импорте {sheet_name}: {str(e)}"
            logger.error(error_msg, exc_info=True)

            if reply:
                await reply.edit_text(error_msg)
            else:
                await message.reply(error_msg)

    async def handle_upconfig(self, message: types.Message):
        """Обработчик команды &upconfig для обновления конфигурации из Google Sheets"""
        try:
            reply = await message.reply("🔄 Начинаю обновление конфигурации из Google Sheets...")
            config_dict = await ConfigImporter.import_config()

            if not config_dict:
                await reply.edit_text("❌ Не удалось загрузить конфигурацию или лист Config пуст.")
                return

            # Обновляем переменные в модуле config
            ConfigImporter.update_config_module(config_dict)

            # Обновляем переменные в GlobalVariables
            from variables import GlobalVariables

            variables_to_update = {
                'PURCHASE_BONUSES': 'purchase_bonuses',
                'STRATEGY_COEFFICIENTS': 'strategy_coefficients',
                'TRANSFER_BONUS': 'transfer_bonus',
                'SOCIAL_LINKS': 'social_links',
                'FAQ_URL': 'faq_url',
                'REQUIRED_CHANNELS': 'required_channels',
                'PROJECT_DOCUMENTS': 'project_documents'
            }

            global_vars = GlobalVariables()
            for config_name, var_name in variables_to_update.items():
                if config_name in config_dict:
                    global_vars.set_static_variable(var_name, config_dict[config_name])

            # Формируем отчет об обновлении
            config_items = []
            for key, value in config_dict.items():
                if isinstance(value, dict) or isinstance(value, list):
                    value_str = f"<структура данных ({type(value).__name__})>"
                else:
                    value_str = str(value)
                    if len(value_str) > 50:
                        value_str = value_str[:47] + "..."
                config_items.append(f"• {key}: {value_str}")

            config_text = "\n".join(config_items)
            await reply.edit_text(
                f"✅ Конфигурация успешно обновлена!\n\n"
                f"Загруженные переменные:\n{config_text}"
            )
            logger.info(f"Configuration updated by admin {message.from_user.id}")

        except Exception as e:
            error_msg = f"❌ Ошибка при обновлении конфигурации: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await message.reply(error_msg)

    async def handle_testmail(self, message: types.Message):
        """Обработчик команды &testmail для тестирования email"""
        try:
            reply = await message.reply("🔄 Тестируем Postmark...")

            from email_sender import email_manager

            # Проверяем подключение
            connected = await email_manager.test_connection()
            if not connected:
                await reply.edit_text("❌ Не могу подключиться к Postmark. Проверьте POSTMARK_API_TOKEN")
                return

            # Пробуем отправить тестовое письмо самому админу
            with Session() as session:
                user = session.query(User).filter_by(telegramID=message.from_user.id).first()
                if not user or not user.email:
                    await reply.edit_text("❌ У вас не указан email. Сначала заполните данные через /fill_user_data")
                    return

                # Модифицируем email_manager для получения подробной ошибки
                try:
                    success = await email_manager.send_notification_email(
                        to=user.email,
                        subject="Talentir Test Email",
                        body="<h1>Тест Postmark</h1><p>Если вы видите это письмо - email работает!</p>"
                    )

                    if success:
                        await reply.edit_text(f"✅ Тестовое письмо отправлено на {user.email}")
                    else:
                        # Получаем последнюю ошибку из логов или делаем дополнительный запрос
                        error_details = await self._get_postmark_error_details(user.email)
                        await reply.edit_text(f"❌ Ошибка отправки на {user.email}\n\n{error_details}")

                except Exception as send_error:
                    await reply.edit_text(f"❌ Ошибка отправки на {user.email}\n\nПодробности: {str(send_error)}")

        except Exception as e:
            await message.reply(f"❌ Ошибка: {str(e)}")
            logger.error(f"Error in testmail command: {e}", exc_info=True)

    async def _get_postmark_error_details(self, email: str) -> str:
        """Получает подробности ошибки Postmark"""
        try:
            from email_sender import email_manager

            # Делаем тестовый запрос для получения конкретной ошибки
            import aiohttp

            data = {
                "From": f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>",
                "To": email,
                "Subject": "Test",
                "HtmlBody": "<p>Test</p>",
                "MessageStream": "outbound"
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{email_manager.provider.base_url}/email",
                        headers=email_manager.provider.headers,
                        json=data
                ) as response:
                    response_data = await response.json()

                    if response.status != 200:
                        error_code = response_data.get('ErrorCode', 'Unknown')
                        error_message = response_data.get('Message', 'Unknown error')

                        # Специальные сообщения для частых ошибок
                        if error_code == 412:
                            return f"🔒 Аккаунт Postmark не подтвержден\n\nОшибка {error_code}: {error_message}\n\n💡 Решение: Подтвердите аккаунт в панели Postmark или используйте email на домене talentir.info"
                        elif error_code == 422:
                            return f"📧 Домен отправителя не верифицирован\n\nОшибка {error_code}: {error_message}\n\n💡 Решение: Верифицируйте домен talentir.info в Postmark"
                        elif error_code == 10:
                            return f"🔑 Неверный API токен\n\nОшибка {error_code}: {error_message}\n\n💡 Решение: Проверьте POSTMARK_API_TOKEN в .env"
                        else:
                            return f"❌ Ошибка {error_code}: {error_message}"

                    return "Неизвестная ошибка"

        except Exception as e:
            return f"Не удалось получить подробности ошибки: {str(e)}"

    async def handle_admin_command(self, message: types.Message, state: FSMContext):
        """Обработчик админских команд"""

        current_state = await state.get_state()
        if current_state:
            await state.finish()
            logger.info(f"Сброшено состояние {current_state} для администратора")

        command = message.text[1:].strip().lower()
        logger.info(f"Processing admin command: {command}")

        if command == "upall":
            try:
                reply = await message.reply("🔄 Начинаю полное обновление данных...")

                # Используем существующую функцию import_all
                results = await import_all(self.dp.bot)

                # Формируем отчет из результатов
                report = []
                for sheet_name, stats in results.items():
                    if isinstance(stats, str):  # Если произошла ошибка
                        report.append(f"\n❌ {sheet_name}: {stats}")
                    else:
                        report.append(f"\n📊 {sheet_name}:")
                        report.append(f"Всего строк: {stats.total}")
                        report.append(f"Обновлено: {stats.updated}")
                        report.append(f"Добавлено: {stats.added}")
                        report.append(f"Пропущено: {stats.skipped}")
                        report.append(f"Ошибок: {stats.errors}")

                        if stats.error_rows:
                            report.append("Ошибки:")
                            for row, error in stats.error_rows:
                                report.append(f"• Строка {row}: {error}")

                await reply.edit_text("✅ Обновление завершено!\n" + "\n".join(report))

            except Exception as e:
                error_msg = f"❌ Критическая ошибка при обновлении: {str(e)}"
                logger.error(error_msg, exc_info=True)
                await message.reply(error_msg)

        elif command == "upuser":
            await self._import_sheet(message, UserImporter, "Users")

        elif command == "upconfig":
            await self.handle_upconfig(message)

        elif command == "upro":
            try:
                clear_template_cache()
                logger.info("BookStack template cache cleared")
                await self._import_sheet(message, ProjectImporter, "Projects")
                await self._import_sheet(message, OptionImporter, "Options")
            except Exception as e:
                error_msg = f"❌ Ошибка при обновлении проектов: {str(e)}"
                logger.error(error_msg, exc_info=True)
                await message.reply(error_msg)

        elif command == "ut":
            try:
                reply = await message.reply("🔄 Обновляю шаблоны...")
                await MessageTemplates.load_templates()
                await reply.edit_text("✅ Шаблоны успешно обновлены")
            except Exception as e:
                error_msg = f"❌ Ошибка обновления шаблонов: {str(e)}"
                logger.error(error_msg, exc_info=True)
                await message.reply(error_msg)

        elif command == "testmail":
            await self.handle_testmail(message)

        elif command == "check":
            try:
                reply = await message.reply("🔍 Проверяю платежи...")

                with Session() as session:
                    # Получаем все неподтвержденные платежи
                    pending_payments = session.query(Payment).filter_by(status="check").all()

                    # Считаем общую сумму неподтвержденных платежей
                    total_amount = session.query(func.sum(Payment.amount)).filter_by(status="check").scalar() or 0

                    # Отправляем информацию о неподтвержденных платежах
                    if pending_payments:
                        report = f"💰 В системе ожидает проверки {len(pending_payments)} платежей на сумму ${total_amount:.2f}"

                        # Удаляем старые уведомления для этих платежей
                        for payment in pending_payments:
                            existing_notifications = (
                                session.query(Notification)
                                    .filter(
                                    Notification.source == "payment_checker",
                                    Notification.text.like(f"%payment_id: {payment.paymentID}%")
                                )
                                    .all()
                            )

                            for notif in existing_notifications:
                                session.delete(notif)

                        session.commit()

                        # Создаем новые уведомления для каждого платежа
                        notifications_created = 0
                        for payment in pending_payments:
                            # Получаем пользователя для этого платежа
                            payer = session.query(User).filter_by(userID=payment.userID).first()
                            if not payer:
                                continue

                            try:
                                # Импортируем функцию создания уведомлений
                                # Важно: это должно быть здесь, чтобы избежать циклических импортов
                                from main import create_payment_check_notification
                                await create_payment_check_notification(payment, payer)
                                notifications_created += 1
                            except Exception as e:
                                logger.error(f"Error creating notification for payment {payment.paymentID}: {e}",
                                             exc_info=True)

                        report += f"\n✅ Создано {notifications_created} новых уведомлений для администраторов"
                        await reply.edit_text(report)
                    else:
                        await reply.edit_text("✅ Непроверенных платежей нет")

            except Exception as e:
                error_msg = f"❌ Ошибка при проверке платежей: {str(e)}"
                logger.error(error_msg, exc_info=True)
                await message.reply(error_msg)

        else:
            await message.reply(f"❌ Неизвестная команда: &{command}")


def setup_admin_commands(dp):
    dp.filters_factory.bind(AdminFilter)
    admin_commands = AdminCommands(dp)

    # Регистрируем middleware
    admin_middleware = AdminCommandsMiddleware(admin_commands)
    dp.middleware.setup(admin_middleware)

    logger.info("Admin commands initialized with middleware")