import logging
import asyncio
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
from datetime import datetime

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
        """Обработчик команды &testmail для тестирования всех email провайдеров"""
        try:
            reply = await message.reply("🔄 Тестируем email систему...")

            from email_sender import email_manager

            # Проверяем наличие провайдеров
            if not email_manager.providers:
                await reply.edit_text("❌ Нет настроенных email провайдеров!\n\n"
                                      "Проверьте настройки:\n"
                                      "• POSTMARK_API_TOKEN\n"
                                      "• SMTP_HOST, SMTP_USER, SMTP_PASSWORD")
                return

            # Показываем список провайдеров
            provider_list = []
            for i, provider in enumerate(email_manager.providers):
                provider_list.append(f"{i + 1}. {provider.__class__.__name__}")

            provider_text = "\n".join(provider_list)
            await reply.edit_text(
                f"📋 Найдено провайдеров: {len(email_manager.providers)}\n{provider_text}\n\n🔗 Тестируем подключения...")

            # Получаем детальный статус всех провайдеров
            providers_status = await email_manager.get_providers_status()

            # Формируем отчет о статусе
            status_report = []
            working_providers = 0

            for provider_name, is_working in providers_status.items():
                if is_working:
                    status_report.append(f"✅ {provider_name}: OK")
                    working_providers += 1
                else:
                    status_report.append(f"❌ {provider_name}: FAILED")

            status_text = "\n".join(status_report)

            if working_providers == 0:
                await reply.edit_text(f"❌ Все провайдеры недоступны!\n\n{status_text}\n\n"
                                      "Проверьте:\n"
                                      "• Postmark API токен\n"
                                      "• SMTP настройки\n"
                                      "• Интернет соединение")
                return

            # Получаем email админа для тестирования
            with Session() as session:
                user = session.query(User).filter_by(telegramID=message.from_user.id).first()
                if not user or not user.email:
                    await reply.edit_text(f"📊 Статус провайдеров:\n{status_text}\n\n"
                                          f"✅ Работающих: {working_providers}/{len(providers_status)}\n\n"
                                          "❌ Не могу отправить тест-письмо!\n"
                                          "У вас не указан email. Заполните данные через /fill_user_data")
                    return

                # Отправляем тестовое письмо
                await reply.edit_text(f"📊 Статус провайдеров:\n{status_text}\n\n"
                                      f"✅ Работающих: {working_providers}/{len(providers_status)}\n\n"
                                      f"📤 Отправляем тест-письмо на {user.email}...")

                try:
                    success = await email_manager.send_notification_email(
                        to=user.email,
                        subject="🧪 Talentir Email System Test",
                        body=f"""
                        <html>
                        <body>
                            <h2>🎉 Email система работает!</h2>
                            <p>Привет, <strong>{user.firstname}</strong>!</p>
                            <p>Если вы видите это письмо, значит наша email система функционирует корректно.</p>

                            <hr>

                            <h3>📊 Информация о системе:</h3>
                            <ul>
                                <li><strong>Провайдеров настроено:</strong> {len(email_manager.providers)}</li>
                                <li><strong>Работающих провайдеров:</strong> {working_providers}</li>
                                <li><strong>Время тестирования:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</li>
                            </ul>

                            <h3>🔧 Статус провайдеров:</h3>
                            <ul>
                                {"".join([f"<li>{'✅' if status else '❌'} {name}</li>" for name, status in providers_status.items()])}
                            </ul>

                            <hr>
                            <p><small>Это автоматически сгенерированное письмо от Talentir Bot.</small></p>
                        </body>
                        </html>
                        """
                    )

                    if success:
                        await reply.edit_text(f"🎯 Email система протестирована!\n\n"
                                              f"📊 Статус провайдеров:\n{status_text}\n\n"
                                              f"✅ Работающих: {working_providers}/{len(providers_status)}\n\n"
                                              f"📧 Тест-письмо отправлено на {user.email}\n"
                                              f"📬 Проверьте почту (включая спам)!")
                    else:
                        # Получаем подробности ошибки
                        error_details = await self._get_email_error_details(user.email, providers_status)
                        await reply.edit_text(f"📊 Статус провайдеров:\n{status_text}\n\n"
                                              f"✅ Работающих: {working_providers}/{len(providers_status)}\n\n"
                                              f"❌ Ошибка отправки на {user.email}\n\n"
                                              f"{error_details}")

                except Exception as send_error:
                    await reply.edit_text(f"📊 Статус провайдеров:\n{status_text}\n\n"
                                          f"✅ Работающих: {working_providers}/{len(providers_status)}\n\n"
                                          f"❌ Критическая ошибка отправки на {user.email}\n\n"
                                          f"Подробности: {str(send_error)}")

        except Exception as e:
            await message.reply(f"❌ Ошибка тестирования email системы: {str(e)}")
            logger.error(f"Error in testmail command: {e}", exc_info=True)

    async def _get_email_error_details(self, email: str, providers_status: dict) -> str:
        """Получает подробности ошибки email отправки"""
        try:
            details = ["🔍 Диагностика ошибки:"]

            # Анализ статуса провайдеров
            working_count = sum(1 for status in providers_status.values() if status)

            if working_count == 0:
                details.append("• Все провайдеры недоступны")
                details.append("• Проверьте настройки API токенов")
                details.append("• Проверьте интернет соединение")
            else:
                details.append(f"• {working_count} провайдер(ов) доступны")
                details.append("• Возможная проблема с email адресом")

            # Проверка конфигурации
            config_issues = []
            if not hasattr(config, 'POSTMARK_API_TOKEN') or not config.POSTMARK_API_TOKEN:
                config_issues.append("POSTMARK_API_TOKEN не настроен")

            if not (hasattr(config, 'SMTP_HOST') and config.SMTP_HOST):
                config_issues.append("SMTP_HOST не настроен")

            if config_issues:
                details.append("\n⚙️ Проблемы конфигурации:")
                for issue in config_issues:
                    details.append(f"• {issue}")

            # Рекомендации
            details.append("\n💡 Рекомендации:")
            details.append("1. Проверьте .env файл")
            details.append("2. Перезапустите бот после изменений")
            details.append("3. Проверьте логи: journalctl -u talentir-bot -f")

            return "\n".join(details)

        except Exception as e:
            return f"Не удалось получить подробности ошибки: {str(e)}"

    async def handle_testsmtp(self, message: types.Message):
        """Обработчик команды &testsmtp для диагностики SMTP подключения"""
        try:
            reply = await message.reply("🔧 Диагностика SMTP подключения...")

            # Проверяем конфигурацию
            smtp_config = {
                'host': getattr(config, 'SMTP_HOST', 'не установлен'),
                'port': getattr(config, 'SMTP_PORT', 'не установлен'),
                'user': getattr(config, 'SMTP_USER', 'не установлен'),
                'password': '***' if hasattr(config, 'SMTP_PASSWORD') and config.SMTP_PASSWORD else 'не установлен'
            }

            config_text = "\n".join([f"• {k}: {v}" for k, v in smtp_config.items()])
            await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n🔌 Проверяем подключение...")

            # Пробуем подключиться напрямую
            import aiosmtplib

            try:
                smtp = aiosmtplib.SMTP(
                    hostname=config.SMTP_HOST,
                    port=config.SMTP_PORT,
                    timeout=10
                )

                await smtp.connect()
                await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n"
                                      f"✅ Подключение установлено\n\n"
                                      f"🔐 Пробуем аутентификацию...")

                try:
                    # Пробуем starttls только если соединение не защищено
                    if not smtp.is_connected_using_tls:
                        await smtp.starttls()
                    await smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
                    await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n"
                                          f"✅ Подключение установлено\n"
                                          f"✅ Аутентификация успешна\n\n"
                                          f"📨 Отправляем тестовое письмо...")

                    # Создаем тестовое письмо
                    from email.mime.text import MIMEText

                    # Получаем email админа
                    with Session() as session:
                        admin_user = session.query(User).filter_by(telegramID=message.from_user.id).first()
                        if not admin_user or not admin_user.email:
                            await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n"
                                                  f"✅ Подключение установлено\n"
                                                  f"✅ Аутентификация успешна\n\n"
                                                  f"❌ У вас не указан email!")
                            await smtp.quit()
                            return

                    message_obj = MIMEText("SMTP Direct Test from Talentir", 'plain', 'utf-8')
                    message_obj['From'] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
                    message_obj['To'] = admin_user.email
                    message_obj['Subject'] = "SMTP Direct Test"

                    await smtp.send_message(message_obj)
                    await smtp.quit()

                    await reply.edit_text(f"🎉 **SMTP полностью работает!**\n\n"
                                          f"📋 Конфигурация:\n{config_text}\n\n"
                                          f"✅ Подключение установлено\n"
                                          f"✅ Аутентификация успешна\n"
                                          f"✅ Письмо отправлено на {admin_user.email}\n\n"
                                          f"📬 Проверьте почту!")

                except aiosmtplib.SMTPAuthenticationError as e:
                    await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n"
                                          f"✅ Подключение установлено\n"
                                          f"❌ Ошибка аутентификации:\n{str(e)}\n\n"
                                          f"Проверьте:\n"
                                          f"• Правильность логина/пароля\n"
                                          f"• Существует ли пользователь {config.SMTP_USER}")

                except Exception as e:
                    await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n"
                                          f"✅ Подключение установлено\n"
                                          f"❌ Ошибка при отправке: {str(e)}")

            except asyncio.TimeoutError:
                await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n"
                                      f"❌ Таймаут подключения к {config.SMTP_HOST}:{config.SMTP_PORT}\n\n"
                                      f"Возможные причины:\n"
                                      f"• Неверный хост или порт\n"
                                      f"• Блокировка фаерволом\n"
                                      f"• SMTP сервер не запущен")

            except Exception as e:
                await reply.edit_text(f"📋 Конфигурация SMTP:\n{config_text}\n\n"
                                      f"❌ Ошибка подключения: {str(e)}")

        except Exception as e:
            await message.reply(f"❌ Критическая ошибка: {str(e)}")
            logger.error(f"Error in testsmtp command: {e}", exc_info=True)

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

        elif command == "testsmtp":
            await self.handle_testsmtp(message)

        elif command == "legacy":

            try:

                reply = await message.reply("🔄 Проверяю legacy миграцию...")

                from legacy_user_processor import legacy_processor

                # Запускаем одну итерацию проверки

                stats = await legacy_processor._process_legacy_users()

                # Формируем подробный отчет

                report = f"📊 Legacy Migration Report:\n\n"

                report += f"📋 Total records: {stats['total_records']}\n"

                report += f"👤 Users found: {stats['users_found']}\n"

                report += f"👥 Upliners assigned: {stats['upliners_assigned']}\n"

                report += f"📈 Purchases created: {stats['purchases_created']}\n"

                report += f"✅ Completed: {stats['completed']}\n"

                report += f"❌ Errors: {stats['errors']}\n\n"

                if stats['users_found'] == 0 and stats['upliners_assigned'] == 0 and stats['purchases_created'] == 0:

                    report += "🔍 No new legacy users found to process."

                else:

                    report += "🎯 Legacy migration processing completed!"

                await reply.edit_text(report)


            except Exception as e:

                error_msg = f"❌ Ошибка при проверке legacy миграции: {str(e)}"

                logger.error(error_msg, exc_info=True)

                await message.reply(error_msg)

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