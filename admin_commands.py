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

from database import Option, Notification, User, Bonus, Project, Purchase, ActiveBalance, PassiveBalance
from bonus_processor import process_purchase_with_bonuses
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

    async def handle_clearprojects(self, message: types.Message):
        """Команда &clearprojects - полная очистка и переимпорт проектов"""
        reply = await message.reply("⚠️ ВНИМАНИЕ! Очищаю таблицу projects...")

        try:
            with Session() as session:
                # Отключаем проверку внешних ключей
                session.execute("PRAGMA foreign_keys = OFF")

                # Удаляем все проекты
                session.query(Project).delete()
                session.commit()

                # Включаем проверку обратно
                session.execute("PRAGMA foreign_keys = ON")

                await reply.edit_text("✅ Таблица projects очищена. Начинаю импорт...")

                # Импортируем новые данные
                sheets_client, _ = get_google_services()
                sheet = sheets_client.open_by_key(config.GOOGLE_SHEET_ID).worksheet("Projects")

                importer = ProjectImporter()
                stats = await importer.import_sheet(sheet)

                report = (
                    f"✅ Импорт завершен:\n"
                    f"Всего строк: {stats.total}\n"
                    f"Добавлено: {stats.added}\n"
                    f"Ошибок: {stats.errors}"
                )

                await reply.edit_text(report)

        except Exception as e:
            error_msg = f"❌ Ошибка: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await reply.edit_text(error_msg)

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

            # Reload secure domains in EmailManager after config update
            try:
                from email_sender import email_manager
                email_manager.reload_secure_domains()
                logger.info("Email secure domains reloaded after config update")
            except Exception as e:
                logger.warning(f"Could not reload email secure domains: {e}")

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
        """Test email functionality with smart provider selection"""
        reply = await message.reply("🔄 Проверяем email систему...")

        try:
            # Parse command: &testmail [email] [provider]
            parts = message.text.split()
            custom_email = None
            forced_provider = None

            if len(parts) > 1:
                custom_email = parts[1]
            if len(parts) > 2:
                forced_provider = parts[2].lower()
                if forced_provider not in ['smtp', 'mailgun']:
                    await reply.edit_text(f"❌ Неизвестный провайдер: {forced_provider}\n"
                                          f"Доступны: smtp, mailgun")
                    return

            # Validate custom email if provided
            if custom_email:
                import re
                if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', custom_email):
                    await reply.edit_text(f"❌ Некорректный email: {custom_email}")
                    return

            # 1. Check providers configuration
            from email_sender import email_manager

            if not email_manager.providers:
                await reply.edit_text("❌ Email провайдеры не настроены!\nПроверьте .env файл")
                return

            # 2. Test all providers
            await reply.edit_text("📊 Проверяем провайдеры...")

            providers_status = await email_manager.get_providers_status()

            status_text = []
            working_providers = []

            for provider_name, is_working in providers_status.items():
                if provider_name == 'smtp':
                    config_info = f"({config.SMTP_HOST}:{config.SMTP_PORT})"
                elif provider_name == 'mailgun':
                    config_info = f"({config.MAILGUN_DOMAIN}, {config.MAILGUN_REGION})"
                else:
                    config_info = ""

                status = "✅ OK" if is_working else "❌ FAIL"
                status_text.append(f"• {provider_name.upper()} {config_info}: {status}")

                if is_working:
                    working_providers.append(provider_name)

            status_report = "\n".join(status_text)

            # 3. Check secure domains configuration
            secure_domains_info = f"📋 Секурные домены: {', '.join(email_manager.secure_domains) if email_manager.secure_domains else 'не настроены'}"

            # 4. Determine target email
            with Session() as session:
                if custom_email:
                    target_email = custom_email
                    firstname = "Test User"
                else:
                    user = session.query(User).filter_by(telegramID=message.from_user.id).first()
                    if not user or not user.email:
                        await reply.edit_text(
                            f"📊 **Статус провайдеров:**\n{status_report}\n\n"
                            f"{secure_domains_info}\n\n"
                            f"❌ У вас не указан email!\n\n"
                            f"Используйте: `&testmail email@example.com [smtp|mailgun]`"
                        )
                        return
                    target_email = user.email
                    firstname = user.firstname

            # 5. Determine which provider will be used
            if forced_provider:
                # Use forced provider
                if forced_provider not in working_providers:
                    await reply.edit_text(
                        f"📊 **Статус провайдеров:**\n{status_report}\n\n"
                        f"❌ Провайдер {forced_provider.upper()} не работает!"
                    )
                    return
                selected_provider = forced_provider
                selection_reason = "форсирован пользователем"
            else:
                # Use smart selection
                provider_order = email_manager._select_provider_for_email(target_email)
                if not provider_order:
                    await reply.edit_text(
                        f"📊 **Статус провайдеров:**\n{status_report}\n\n"
                        f"❌ Нет доступных провайдеров для {target_email}"
                    )
                    return

                selected_provider = provider_order[0]
                domain = email_manager._get_email_domain(target_email)

                if domain in email_manager.secure_domains:
                    selection_reason = f"домен {domain} в списке секурных"
                else:
                    selection_reason = f"домен {domain} обычный"

            # 6. Send test email
            await reply.edit_text(
                f"📊 **Статус провайдеров:**\n{status_report}\n\n"
                f"{secure_domains_info}\n\n"
                f"📤 Отправляем тест на {target_email}\n"
                f"🎯 Используем: **{selected_provider.upper()}** ({selection_reason})..."
            )

            # Prepare test email
            test_html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; border-radius: 10px 10px 0 0;">
                    <h1 style="color: white; margin: 0;">🚀 JetUp Email Test</h1>
                </div>

                <div style="padding: 20px; background: #f5f5f5;">
                    <p>Привет, <strong>{firstname}</strong>!</p>

                    <div style="background: white; padding: 15px; border-radius: 8px; margin: 15px 0;">
                        <h3 style="color: #667eea; margin-top: 0;">📊 Детали отправки:</h3>
                        <ul style="list-style-type: none; padding-left: 0;">
                            <li>📧 <strong>Получатель:</strong> {target_email}</li>
                            <li>🔧 <strong>Провайдер:</strong> {selected_provider.upper()}</li>
                            <li>📝 <strong>Причина выбора:</strong> {selection_reason}</li>
                            <li>⏰ <strong>Время:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</li>
                        </ul>
                    </div>

                    <div style="background: #e8f4fd; padding: 15px; border-radius: 8px; border-left: 4px solid #2196F3;">
                        <p style="margin: 0;"><strong>✅ Email система работает корректно!</strong></p>
                        <p style="margin: 5px 0 0 0; font-size: 14px; color: #666;">
                            Это письмо отправлено через {selected_provider.upper()} провайдер.
                        </p>
                    </div>
                </div>

                <div style="padding: 15px; background: #333; color: #999; font-size: 12px; text-align: center;">
                    <p style="margin: 0;">JetUp Investment Bot | Automated Test Email</p>
                </div>
            </body>
            </html>
            """

            # Send through selected provider directly
            provider = email_manager.providers[selected_provider]
            success = await provider.send_email(
                to=target_email,
                subject=f"✅ Test Email via {selected_provider.upper()} | JetUp",
                html_body=test_html,
                text_body=None
            )

            if success:
                fallback_info = ""
                if len(provider_order) > 1:
                    fallback_info = f"\n💡 Fallback провайдер: {provider_order[1].upper()}"

                await reply.edit_text(
                    f"🎉 **Email система работает!**\n\n"
                    f"📊 **Статус провайдеров:**\n{status_report}\n\n"
                    f"{secure_domains_info}\n\n"
                    f"✅ **Тест отправлен:**\n"
                    f"• Получатель: {target_email}\n"
                    f"• Провайдер: {selected_provider.upper()}\n"
                    f"• Причина: {selection_reason}{fallback_info}\n\n"
                    f"📬 Проверьте почту (включая папку спам)!"
                )
            else:
                await reply.edit_text(
                    f"⚠️ **Ошибка отправки**\n\n"
                    f"📊 **Статус провайдеров:**\n{status_report}\n\n"
                    f"{secure_domains_info}\n\n"
                    f"❌ Не удалось отправить через {selected_provider.upper()}\n\n"
                    f"Проверьте логи: `journalctl -u jetup-bot -f`"
                )

        except Exception as e:
            await message.reply(f"❌ Критическая ошибка: {str(e)}")
            logger.error(f"Error in testmail command: {e}", exc_info=True)

    async def handle_addtokens(self, message: types.Message):
        """Handler for &addtokens command to manually add shares to user"""
        try:
            # Parse command arguments
            command_text = message.text[1:].strip()  # Remove & and whitespace

            # Expected format: addtokens u:{userID} pj:{projectID} q:{Qty} o:{OptionID} (optional)
            if not command_text.startswith('addtokens'):
                await message.reply("❌ Invalid command format")
                return

            # Extract parameters using regex
            import re

            # Parse parameters
            user_match = re.search(r'u:(\d+)', command_text)
            project_match = re.search(r'pj:(\d+)', command_text)
            qty_match = re.search(r'q:(\d+)', command_text)
            option_match = re.search(r'o:(\d+)', command_text)

            if not all([user_match, project_match, qty_match]):
                await message.reply(
                    "❌ Invalid command format!\n\n"
                    "Usage: &addtokens u:{userID} pj:{projectID} q:{Qty} o:{OptionID}\n"
                    "       &addtokens u:{userID} pj:{projectID} q:{Qty}\n\n"
                    "Examples:\n"
                    "  &addtokens u:123 pj:42 q:100 o:456\n"
                    "  &addtokens u:123 pj:42 q:100"
                )
                return

            # Extract values
            user_id = int(user_match.group(1))
            project_id = int(project_match.group(1))
            quantity = int(qty_match.group(1))
            option_id = int(option_match.group(1)) if option_match else None

            # Validate parameters
            if quantity <= 0:
                await message.reply("❌ Quantity must be positive")
                return

            reply = await message.reply(f"🔄 Processing manual share addition...")

            with Session() as session:
                # Check if user exists
                target_user = session.query(User).filter_by(userID=user_id).first()
                if not target_user:
                    await reply.edit_text(f"❌ User with ID {user_id} not found")
                    return

                # Check if project exists
                project = session.query(Project).filter_by(projectID=project_id).first()
                if not project:
                    await reply.edit_text(f"❌ Project with ID {project_id} not found")
                    return

                # Find option
                if option_id:
                    # Use specified option
                    option = session.query(Option).filter_by(
                        optionID=option_id,
                        projectID=project_id
                    ).first()

                    if not option:
                        await reply.edit_text(
                            f"❌ Option with ID {option_id} not found for project {project_id}"
                        )
                        return

                    # Check if specified quantity matches option or is within reasonable bounds
                    if quantity != option.packQty:
                        await reply.edit_text(
                            f"⚠️ Warning: Specified quantity ({quantity}) differs from option quantity ({option.packQty})\n"
                            f"Proceeding with specified quantity: {quantity}"
                        )

                else:
                    # Find first available option for this project (preferably active)
                    option = session.query(Option).filter_by(
                        projectID=project_id,
                        isActive=True
                    ).order_by(Option.optionID.asc()).first()

                    if not option:
                        # If no active options, try inactive ones
                        option = session.query(Option).filter_by(
                            projectID=project_id
                        ).order_by(Option.optionID.asc()).first()

                    if not option:
                        await reply.edit_text(
                            f"❌ No options found for project {project_id}\n"
                            f"Please create an option first or specify option ID"
                        )
                        return

                    # Inform about auto-selected option
                    await reply.edit_text(
                        f"🔄 Auto-selected option {option.optionID} for project {project_id}\n"
                        f"Option: {option.packQty} shares at ${option.costPerShare:.2f} per share\n"
                        f"Processing {quantity} shares..."
                    )

                # Calculate total price based on option's price per share
                total_price = option.costPerShare * quantity

                # Get admin user for logging
                admin_user = session.query(User).filter_by(telegramID=message.from_user.id).first()
                admin_name = admin_user.firstname if admin_user else "Unknown Admin"

                # Create purchase record using existing option
                purchase = Purchase(
                    userID=user_id,
                    projectID=project_id,
                    projectName=project.projectName,
                    optionID=option.optionID,
                    packQty=quantity,
                    packPrice=total_price,
                    createdAt=datetime.utcnow()
                )

                session.add(purchase)
                session.flush()  # Get the purchase ID

                # Create ActiveBalance record for tracking (no actual balance change)
                balance_record = ActiveBalance(
                    userID=user_id,
                    firstname=target_user.firstname,
                    surname=target_user.surname,
                    amount=0.0,  # No balance change for manual addition
                    status='done',
                    reason=f'manual_addition={purchase.purchaseID}',
                    link='',
                    notes=f'Manual share addition by admin: {admin_name} ({message.from_user.id}). '
                          f'Option: {option.optionID}, Qty: {quantity}, Price: ${total_price:.2f}'
                )
                session.add(balance_record)

                # Create notification for the user
                text, buttons = await MessageTemplates.get_raw_template(
                    'admin_tokens_added_notification',
                    {
                        'firstname': target_user.firstname,
                        'quantity': quantity,
                        'project_name': project.projectName,
                        'price': total_price,
                        'admin_name': admin_name
                    },
                    lang=target_user.lang
                )

                user_notification = Notification(
                    source="admin_command",
                    text=text,
                    buttons=buttons,
                    target_type="user",
                    target_value=str(user_id),
                    priority=2,
                    category="admin",
                    importance="high",
                    parse_mode="HTML"
                )
                session.add(user_notification)

                # Create notification for other admins
                admin_text, admin_buttons = await MessageTemplates.get_raw_template(
                    'admin_tokens_added_admin_notification',
                    {
                        'admin_name': admin_name,
                        'admin_id': message.from_user.id,
                        'user_name': target_user.firstname,
                        'user_id': user_id,
                        'quantity': quantity,
                        'project_name': project.projectName,
                        'price': total_price,
                        'purchase_id': purchase.purchaseID,
                        'option_id': option.optionID
                    }
                )

                # Send to all admins except the one who executed the command
                for admin_id in config.ADMIN_USER_IDS:
                    if admin_id != (admin_user.userID if admin_user else None):
                        admin_notification = Notification(
                            source="admin_command",
                            text=admin_text,
                            buttons=admin_buttons,
                            target_type="user",
                            target_value=str(admin_id),
                            priority=1,
                            category="admin",
                            importance="normal",
                            parse_mode="HTML"
                        )
                        session.add(admin_notification)

                session.commit()

                # NOTE: No referral bonuses for manual admin additions
                # asyncio.create_task(process_purchase_with_bonuses(purchase.purchaseID))

                # Success message
                option_status = "🟢 Active" if option.isActive else "🔴 Inactive"
                await reply.edit_text(
                    f"✅ Successfully added shares!\n\n"
                    f"👤 User: {target_user.firstname} (ID: {user_id})\n"
                    f"📊 Project: {project.projectName} (ID: {project_id})\n"
                    f"🎯 Quantity: {quantity} shares\n"
                    f"💰 Total Price: ${total_price:.2f}\n"
                    f"🔧 Option: {option.optionID} ({option_status})\n"
                    f"💵 Price per share: ${option.costPerShare:.2f}\n"
                    f"🆔 Purchase ID: {purchase.purchaseID}\n\n"
                    f"📬 User has been notified\n"
                    f"⚠️ No referral bonuses will be processed for manual additions"
                )

                logger.info(f"Manual shares added by admin {message.from_user.id}: "
                            f"User {user_id}, Project {project_id}, Option {option.optionID}, "
                            f"Qty {quantity}, Total ${total_price:.2f}")

        except ValueError as e:
            await message.reply(f"❌ Invalid parameter format: {str(e)}")
        except Exception as e:
            logger.error(f"Error in addtokens command: {e}", exc_info=True)
            await message.reply(f"❌ Error adding shares: {str(e)}")

    async def handle_delpurchase(self, message: types.Message):
        """Handler for &delpurchase command to safely delete purchase records"""
        try:
            # Parse command arguments
            command_parts = message.text.strip().split()

            if len(command_parts) != 2:
                await message.reply(
                    "❌ Invalid command format!\n\n"
                    "Usage: &delpurchase {purchaseID}\n\n"
                    "Example: &delpurchase 123"
                )
                return

            try:
                purchase_id = int(command_parts[1])
            except ValueError:
                await message.reply("❌ Purchase ID must be a number")
                return

            reply = await message.reply(f"🔄 Analyzing purchase {purchase_id}...")

            with Session() as session:
                # First, get purchase details
                purchase = session.query(Purchase).filter_by(purchaseID=purchase_id).first()

                if not purchase:
                    await reply.edit_text(f"❌ Purchase {purchase_id} not found")
                    return

                # Get user info
                user = session.query(User).filter_by(userID=purchase.userID).first()
                user_name = user.firstname if user else "Unknown"

                # Check for related records
                related_bonuses = session.query(Bonus).filter_by(purchaseID=purchase_id).all()
                related_active_balance = session.query(ActiveBalance).filter(
                    ActiveBalance.reason.like(f'%purchase={purchase_id}%')
                ).all()
                related_passive_balance = session.query(PassiveBalance).filter(
                    PassiveBalance.reason.like(f'%bonus=%')
                ).join(Bonus).filter(Bonus.purchaseID == purchase_id).all()

                # Show analysis
                analysis = (
                    f"📊 Purchase Analysis:\n\n"
                    f"🆔 Purchase ID: {purchase_id}\n"
                    f"👤 User: {user_name} (ID: {purchase.userID})\n"
                    f"📊 Project: {purchase.projectName} (ID: {purchase.projectID})\n"
                    f"🎯 Quantity: {purchase.packQty} shares\n"
                    f"💰 Price: ${purchase.packPrice:.2f}\n"
                    f"🔧 Option: {purchase.optionID}\n"
                    f"📅 Date: {purchase.createdAt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"🔗 Related Records:\n"
                    f"• Bonuses: {len(related_bonuses)}\n"
                    f"• Active Balance: {len(related_active_balance)}\n"
                    f"• Passive Balance: {len(related_passive_balance)}\n\n"
                )

                if related_bonuses:
                    total_bonuses = sum(b.bonusAmount for b in related_bonuses)
                    analysis += f"💰 Total bonuses paid: ${total_bonuses:.2f}\n"

                analysis += "⚠️ This will permanently delete the purchase and ALL related records!"

                await reply.edit_text(analysis)

                # Wait for confirmation (in real implementation, you'd use FSM or inline keyboard)
                await asyncio.sleep(2)

                confirmation_msg = await message.reply(
                    "⚠️ Are you sure you want to delete this purchase?\n\n"
                    "This action cannot be undone and will:\n"
                    "• Delete the purchase record\n"
                    "• Delete all related bonuses\n"
                    "• Delete related balance records\n"
                    "• Update user balances\n\n"
                    "Reply with 'CONFIRM DELETE' to proceed"
                )

                # In a real implementation, you'd use FSM here
                # For now, let's implement immediate deletion with admin confirmation

        except Exception as e:
            logger.error(f"Error in delpurchase analysis: {e}", exc_info=True)
            await message.reply(f"❌ Error analyzing purchase: {str(e)}")

    async def handle_delpurchase_confirm(self, message: types.Message, purchase_id: int):
        """Actual deletion after confirmation"""
        try:
            reply = await message.reply(f"🔄 Deleting purchase {purchase_id}...")

            with Session() as session:
                # Get admin user for logging
                admin_user = session.query(User).filter_by(telegramID=message.from_user.id).first()
                admin_name = admin_user.firstname if admin_user else "Unknown Admin"

                # Begin transaction
                session.begin()

                try:
                    # Get purchase details before deletion
                    purchase = session.query(Purchase).filter_by(purchaseID=purchase_id).first()
                    if not purchase:
                        await reply.edit_text(f"❌ Purchase {purchase_id} not found")
                        return

                    user = session.query(User).filter_by(userID=purchase.userID).first()

                    # 1. Delete related bonuses and update balances
                    bonuses = session.query(Bonus).filter_by(purchaseID=purchase_id).all()
                    total_bonuses_removed = 0

                    for bonus in bonuses:
                        # Decrease passive balance of bonus recipient
                        bonus_user = session.query(User).filter_by(userID=bonus.userID).first()
                        if bonus_user:
                            bonus_user.balancePassive -= bonus.bonusAmount
                            total_bonuses_removed += bonus.bonusAmount

                            # Create negative passive balance record
                            passive_record = PassiveBalance(
                                userID=bonus_user.userID,
                                firstname=bonus_user.firstname,
                                surname=bonus_user.surname,
                                amount=-bonus.bonusAmount,
                                status='done',
                                reason=f'bonus_removal={bonus.bonusID}',
                                notes=f'Bonus removed due to purchase deletion by admin: {admin_name}'
                            )
                            session.add(passive_record)

                    # Delete bonuses
                    session.query(Bonus).filter_by(purchaseID=purchase_id).delete()

                    # 2. Delete related active balance records
                    active_balance_records = session.query(ActiveBalance).filter(
                        ActiveBalance.reason.like(f'%purchase={purchase_id}%')
                    ).all()

                    balance_adjustment = 0
                    for record in active_balance_records:
                        if record.reason == f'purchase={purchase_id}':
                            # This was the original purchase deduction
                            balance_adjustment = -record.amount  # Restore the balance

                    session.query(ActiveBalance).filter(
                        ActiveBalance.reason.like(f'%purchase={purchase_id}%')
                    ).delete()

                    # 3. Adjust user's active balance if needed
                    if balance_adjustment != 0 and user:
                        user.balanceActive += balance_adjustment

                        # Create balance restoration record
                        restore_record = ActiveBalance(
                            userID=user.userID,
                            firstname=user.firstname,
                            surname=user.surname,
                            amount=balance_adjustment,
                            status='done',
                            reason=f'purchase_deletion={purchase_id}',
                            notes=f'Balance restored due to purchase deletion by admin: {admin_name}'
                        )
                        session.add(restore_record)

                    # 4. Delete the purchase record
                    session.query(Purchase).filter_by(purchaseID=purchase_id).delete()

                    # 5. Create admin log entry
                    admin_log = ActiveBalance(
                        userID=admin_user.userID if admin_user else 0,
                        firstname=admin_name,
                        surname=admin_user.surname if admin_user else '',
                        amount=0.0,
                        status='done',
                        reason=f'admin_deletion={purchase_id}',
                        notes=f'Purchase {purchase_id} deleted by admin. '
                              f'User: {user.firstname} (ID: {user.userID}), '
                              f'Shares: {purchase.packQty}, Price: ${purchase.packPrice:.2f}, '
                              f'Bonuses removed: ${total_bonuses_removed:.2f}'
                    )
                    session.add(admin_log)

                    # Commit transaction
                    session.commit()

                    # Success message
                    await reply.edit_text(
                        f"✅ Purchase {purchase_id} deleted successfully!\n\n"
                        f"📊 Deleted:\n"
                        f"• Purchase: {purchase.packQty} shares of {purchase.projectName}\n"
                        f"• Bonuses: {len(bonuses)} records (${total_bonuses_removed:.2f})\n"
                        f"• Balance records: {len(active_balance_records)}\n\n"
                        f"💰 User balance restored: ${balance_adjustment:.2f}\n"
                        f"👤 Affected user: {user.firstname} (ID: {user.userID})"
                    )

                    # Notify affected users
                    if user and balance_adjustment != 0:
                        text, buttons = await MessageTemplates.get_raw_template(
                            'purchase_deleted_notification',
                            {
                                'firstname': user.firstname,
                                'purchase_id': purchase_id,
                                'shares': purchase.packQty,
                                'project_name': purchase.projectName,
                                'balance_restored': balance_adjustment,
                                'admin_name': admin_name
                            },
                            lang=user.lang
                        )

                        notification = Notification(
                            source="admin_command",
                            text=text,
                            buttons=buttons,
                            target_type="user",
                            target_value=str(user.userID),
                            priority=2,
                            category="admin",
                            importance="high",
                            parse_mode="HTML"
                        )
                        session.add(notification)
                        session.commit()

                    logger.info(f"Purchase {purchase_id} deleted by admin {message.from_user.id}")

                except Exception as e:
                    session.rollback()
                    raise e

        except Exception as e:
            logger.error(f"Error in delpurchase confirm: {e}", exc_info=True)
            await message.reply(f"❌ Error deleting purchase: {str(e)}")

    async def handle_admin_command(self, message: types.Message, state: FSMContext):
        """Обработчик админских команд"""

        current_state = await state.get_state()
        if current_state:
            await state.finish()
            logger.info(f"Сброшено состояние {current_state} для администратора")

        command = message.text[1:].split()[0].lower()
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

        elif command.startswith("addtokens"):
            await self.handle_addtokens(message)

        elif command.startswith("delpurchase"):
            await self.handle_delpurchase(message)

        elif command == "testmail":
            await self.handle_testmail(message)

        elif command == "clearprojects":
            await self.handle_clearprojects(message)

        elif command == "legacy":
            try:
                reply = await message.reply("🔄 Проверяю legacy миграцию...")

                from legacy_user_processor import legacy_processor

                # Запускаем одну итерацию проверки
                stats = await legacy_processor._process_legacy_users()

                # Формируем подробный отчет
                report = f"📊 Legacy Migration Report:\n\n"
                report += f"📋 Total records: {stats.total_records}\n"
                report += f"👤 Users found: {stats.users_found}\n"
                report += f"👥 Upliners assigned: {stats.upliners_assigned}\n"
                report += f"📈 Purchases created: {stats.purchases_created}\n"
                report += f"✅ Completed: {stats.completed}\n"
                report += f"❌ Errors: {stats.errors}\n"

                # Добавляем информацию о дубликатах, если есть
                if hasattr(stats, 'duplicate_purchases_prevented'):
                    report += f"🛡️ Duplicate purchases prevented: {stats.duplicate_purchases_prevented}\n"

                report += "\n"

                if stats.users_found == 0 and stats.upliners_assigned == 0 and stats.purchases_created == 0:
                    report += "🔍 No new legacy users found to process."
                else:
                    report += "🎯 Legacy migration processing completed!"

                # Добавляем детали ошибок если они есть
                if stats.errors > 0 and stats.error_details:
                    report += f"\n\n❌ Error details (showing first 5):\n"
                    for i, (email, error) in enumerate(stats.error_details[:5]):
                        report += f"• {email}: {error}\n"

                    if len(stats.error_details) > 5:
                        report += f"... and {len(stats.error_details) - 5} more errors"

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