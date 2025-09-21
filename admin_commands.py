import logging
import asyncio
import os
from aiogram.dispatcher.filters import Filter
from aiogram.dispatcher import FSMContext
from aiogram import types
import shutil
from typing import Dict
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware
from bookstack_integration import clear_template_cache

import config
from imports import (
    ProjectImporter, UserImporter, OptionImporter,
    ConfigImporter, import_all
)

from sync_system.sync_engine import UniversalSyncEngine
from sync_system.sync_config import SYNC_CONFIG, SUPPORT_TABLES
from database import Bonus, Project, Purchase, ActiveBalance, PassiveBalance
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
    def __init__(self, dp, message_manager):
        self.dp = dp
        self.message_manager = message_manager
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

    async def handle_restore(self, message: types.Message):
        """&restore [backup_file] - восстановление из бэкапа"""
        args = message.text.split()[1:] if len(message.text.split()) > 1 else []

        if not args:
            # Показываем список доступных бэкапов
            backup_dir = "/opt/talentir/backups/import"
            if os.path.exists(backup_dir):
                backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')])[-5:]
                if backups:
                    backup_list = "\n".join(backups)
                    await message.reply(
                        f"📁 Доступные бэкапы:\n{backup_list}\n\n"
                        "Использование: &restore [имя_файла]"
                    )
                else:
                    await message.reply("Нет доступных бэкапов")
            return

        backup_name = args[0]
        backup_path = f"/opt/talentir/backups/import/{backup_name}"

        if not os.path.exists(backup_path):
            await message.reply(f"❌ Бэкап не найден: {backup_name}")
            return

        try:
            # Создаем бэкап текущей БД перед восстановлением
            current_backup = await self._create_backup()

            # Восстанавливаем из бэкапа
            shutil.copy2(backup_path, "/opt/talentir/bot/data/talentir.db")

            await message.reply(
                f"✅ БД восстановлена из бэкапа: {backup_name}\n"
                f"Предыдущее состояние сохранено в: {os.path.basename(current_backup)}"
            )

        except Exception as e:
            await message.reply(f"❌ Ошибка восстановления: {str(e)}")

    async def handle_import(self, message: types.Message):
        """Обработчик команды &import - полный импорт из Google Sheets"""

        # Получаем админа и его язык
        with Session() as session:
            admin_user = session.query(User).filter_by(telegramID=message.from_user.id).first()
            if not admin_user:
                return

        # Парсим параметры команды
        command_parts = message.text.strip().split()
        mode = 'dry'  # По умолчанию
        tables_to_import = SUPPORT_TABLES.copy()

        # Обрабатываем аргументы более гибко
        if len(command_parts) > 1:
            # Собираем все аргументы после команды
            args = command_parts[1:]

            # Ищем режим среди аргументов
            mode_found = False
            remaining_args = []
            for arg in args:
                if arg.lower() in ['dry', 'safe', 'force']:
                    mode = arg.lower()
                    mode_found = True
                else:
                    remaining_args.append(arg)

            # Оставшиеся аргументы - это таблицы
            if remaining_args:
                # Может быть одна таблица или список через запятую
                tables_str = ' '.join(remaining_args)
                requested_tables = [t.strip() for t in tables_str.split(',')]

                valid_tables = []
                for table in requested_tables:
                    # Проверяем с учетом регистра (Users, Payments и т.д.)
                    found = False
                    for supported_table in SUPPORT_TABLES:
                        if table.lower() == supported_table.lower():
                            valid_tables.append(supported_table)
                            found = True
                            break

                    if not found:
                        await self.message_manager.send_template(
                            user=admin_user,
                            template_key='admin/import/unknown_table',
                            variables={'table': table},
                            update=message
                        )
                        return

                if valid_tables:
                    tables_to_import = valid_tables

        # Создаем бэкап если не dry режим
        backup_path = None
        if mode != 'dry':
            try:
                backup_path = await self._create_backup()
                logger.info(f"Created backup: {backup_path}")
            except Exception as e:
                logger.error(f"Failed to create backup: {e}")
                await self.message_manager.send_template(
                    user=admin_user,
                    template_key='admin/import/backup_error',
                    variables={'error': str(e)},
                    update=message
                )
                return

        # Отправляем начальное сообщение
        await self.message_manager.send_template(
            user=admin_user,
            template_key='admin/sync/starting',
            variables={
                'mode': mode,
                'tables': ', '.join(tables_to_import)
            },
            update=message
        )

        all_results = {}
        total_stats = {
            'total': 0,
            'updated': 0,
            'added': 0,
            'skipped': 0,
            'errors': 0
        }

        try:
            with Session() as sync_session:
                for table_name in tables_to_import:
                    try:
                        # Импортируем таблицу (БЕЗ admin_lang!)
                        engine = UniversalSyncEngine(table_name)
                        results = engine.import_from_sheets(
                            sync_session,
                            dry_run=(mode == 'dry')
                        )

                        all_results[table_name] = results

                        # Обновляем общую статистику
                        total_stats['total'] += results.get('total', 0)
                        total_stats['updated'] += results.get('updated', 0)
                        total_stats['added'] += results.get('added', 0)
                        total_stats['skipped'] += results.get('skipped', 0)
                        total_stats['errors'] += len(results.get('errors', []))

                    except Exception as e:
                        logger.error(f"Failed to import {table_name}: {e}")
                        all_results[table_name] = {
                            'error': str(e),
                            'total': 0,
                            'updated': 0,
                            'added': 0,
                            'skipped': 0,
                            'errors': []
                        }
                        total_stats['errors'] += 1

        except Exception as e:
            logger.error(f"Critical import error: {e}")
            await self.message_manager.send_template(
                user=admin_user,
                template_key='admin/sync/critical_error',
                variables={'error': str(e)},
                update=message
            )
            return

        # Формируем детальный отчет
        report = await self._format_detailed_import_report(
            all_results,
            mode,
            backup_path,
            admin_user.lang
        )

        # Отправляем финальный отчет
        if len(report) > 4000:
            # Краткий отчет
            icon = '✅' if total_stats['errors'] == 0 else '⚠️'

            await self.message_manager.send_template(
                user=admin_user,
                template_key=['admin/sync/report_header', 'admin/sync/report_summary'],
                variables={
                    'mode': mode,
                    'icon': icon,
                    'updated': total_stats['updated'],
                    'added': total_stats['added'],
                    'errors': total_stats['errors'],
                    'backup_path': backup_path or ''
                },
                update=message
            )

            # Детальные отчеты по таблицам с ошибками как отдельные сообщения
            for table_name, results in all_results.items():
                if results.get('errors') or results.get('changes'):
                    table_report = await self._format_table_report(
                        table_name,
                        results,
                        mode,
                        admin_user.lang
                    )

                    # Отправляем через message_manager с raw шаблоном
                    await self.message_manager.send_template(
                        user=admin_user,
                        template_key='admin/sync/table_details_raw',
                        variables={'content': table_report},
                        update=message
                    )
        else:
            # Отправляем полный отчет
            await self.message_manager.send_template(
                user=admin_user,
                template_key='admin/sync/full_report',
                variables={'report': report},
                update=message
            )

    async def _format_detailed_import_report(self, results: Dict, mode: str, backup_path: str, lang: str) -> str:
        """Форматирует детальный отчет об импорте с использованием шаблонов"""

        # Заголовок
        header_key = 'admin/sync/report_header_check' if mode == 'dry' else 'admin/sync/report_header_import'
        header, _ = await MessageTemplates.get_raw_template(header_key, {}, lang)

        report = header + "\n" + "=" * 30 + "\n\n"

        # По каждой таблице
        for table_name, result in results.items():
            # Заголовок таблицы
            report += f"📋 {table_name}:\n"

            if 'error' in result:
                # Критическая ошибка таблицы
                error_text, _ = await MessageTemplates.get_raw_template(
                    'admin/sync/table_critical_error',
                    {'error': result['error']},
                    lang
                )
                report += f"  {error_text}\n\n"
            else:
                # Статистика
                stats_text, _ = await MessageTemplates.get_raw_template(
                    'admin/sync/table_stats',
                    {
                        'total': result.get('total', 0),
                        'updated': result.get('updated', 0),
                        'added': result.get('added', 0),
                        'skipped': result.get('skipped', 0)
                    },
                    lang
                )
                report += stats_text + "\n"

                # Предупреждения о балансах
                warnings = result.get('warnings', [])
                if warnings:
                    warnings_header, _ = await MessageTemplates.get_raw_template(
                        'admin/sync/warnings_header',
                        {'count': len(warnings)},
                        lang
                    )
                    report += f"  {warnings_header}\n"

                    for warn in warnings[:5]:  # Первые 5 предупреждений
                        report += f"    • Строка {warn.get('row', '?')}: {warn.get('warning', 'Balance mismatch')}\n"

                # Ошибки
                errors = result.get('errors', [])
                if errors:
                    errors_header, _ = await MessageTemplates.get_raw_template(
                        'admin/sync/errors_header',
                        {'count': len(errors)},
                        lang
                    )
                    report += f"  {errors_header}\n"

                    for err in errors[:5]:  # Первые 5 ошибок
                        report += f"    • Строка {err.get('row', '?')}: {err.get('error', 'Unknown error')}\n"

                # Изменения (только первые 5)
                changes = result.get('changes', [])
                if changes and mode == 'dry':
                    changes_header, _ = await MessageTemplates.get_raw_template(
                        'admin/sync/changes_header',
                        {'count': min(5, len(changes))},
                        lang
                    )
                    report += f"  {changes_header}\n"

                    for change in changes[:5]:
                        action_text, _ = await MessageTemplates.get_raw_template(
                            'admin/sync/change_action_update' if change.get(
                                'action') == 'update' else 'admin/sync/change_action_add',
                            {},
                            lang
                        )
                        report += f"    • ID {change.get('id', '?')}: {action_text}\n"

                        if change.get('action') == 'update':
                            for field_change in change.get('fields', []):
                                field = field_change.get('field')
                                old = field_change.get('old', '')
                                new = field_change.get('new', '')
                                report += f"      {field}: {old} → {new}\n"

            report += "\n"

        if backup_path:
            backup_text, _ = await MessageTemplates.get_raw_template(
                'admin/sync/backup_created',
                {'path': backup_path},
                lang
            )
            report += backup_text

        return report

    async def _create_backup(self) -> str:
        """Создает бэкап БД перед импортом"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Берем директорию из конфига
        backup_dir = config.BACKUP_BASE_DIR
        os.makedirs(backup_dir, exist_ok=True)

        # ИСПРАВЛЕНИЕ: извлекаем путь из DATABASE_URL
        if config.DATABASE_URL.startswith("sqlite:///"):
            db_path = config.DATABASE_URL.replace("sqlite:///", "")
        else:
            raise ValueError(f"Unsupported DATABASE_URL format: {config.DATABASE_URL}")

        # Проверяем что БД существует
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")

        # Копируем БД
        backup_filename = f"talentir_{timestamp}.db"
        backup_path = os.path.join(backup_dir, backup_filename)
        shutil.copy2(db_path, backup_path)

        # Удаляем старые бэкапы (оставляем последние 20)
        backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')])
        if len(backups) > 20:
            for old_backup in backups[:-20]:
                os.remove(os.path.join(backup_dir, old_backup))

        logger.info(f"Created backup: {backup_path}")
        return backup_path

    async def handle_object(self, message: types.Message):
        """Отправляет медиа-объект по его file_id"""
        try:
            # Парсим команду
            parts = message.text.strip().split(maxsplit=1)

            if len(parts) != 2:
                await message.reply(
                    "❌ Неверный формат команды!\n\n"
                    "Использование: &object {file_id}\n\n"
                    "Пример: &object AgACAgIAAxkBAAIhhGgQ228dxkaJG1zUfh61OfzelNVRAAI88DEb-CGJSI454TV0wQSVAQADAgADeQADNgQ"
                )
                return

            file_id = parts[1].strip()

            # Логируем для отладки
            logger.info(f"Attempting to send object with file_id: {file_id}")

            # Пытаемся отправить как разные типы медиа
            # Порядок важен - начинаем с наиболее специфичных типов

            send_attempts = [
                ('sticker', lambda: message.reply_sticker(sticker=file_id)),
                ('photo', lambda: message.reply_photo(photo=file_id, caption="📷 Photo object")),
                ('video', lambda: message.reply_video(video=file_id, caption="🎥 Video object")),
                ('document', lambda: message.reply_document(document=file_id, caption="📄 Document object")),
                ('animation', lambda: message.reply_animation(animation=file_id, caption="🎬 Animation object")),
                ('audio', lambda: message.reply_audio(audio=file_id, caption="🎵 Audio object")),
                ('voice', lambda: message.reply_voice(voice=file_id, caption="🎤 Voice object")),
                ('video_note', lambda: message.reply_video_note(video_note=file_id))
            ]

            errors = []

            for media_type, send_func in send_attempts:
                try:
                    await send_func()
                    # Если успешно отправлено, сообщаем тип и выходим
                    await message.reply(f"✅ Объект успешно отправлен как: **{media_type}**", parse_mode="Markdown")
                    logger.info(f"Successfully sent object as {media_type}")
                    return
                except Exception as e:
                    error_text = str(e)
                    # Сохраняем только уникальные ошибки
                    if not any(error_text in err for err in errors):
                        errors.append(f"{media_type}: {error_text}")
                    continue

            # Если ничего не сработало
            error_report = "❌ Не удалось отправить объект!\n\n"
            error_report += "Возможные причины:\n"
            error_report += "• File_id недействителен или устарел\n"
            error_report += "• Объект из другого бота\n"
            error_report += "• Объект был удален из Telegram\n\n"

            if errors:
                error_report += "Детали ошибок:\n"
                for err in errors[:3]:  # Показываем первые 3 ошибки
                    error_report += f"• {err}\n"

            await message.reply(error_report)
            logger.error(f"Failed to send object {file_id}: all attempts failed")

        except Exception as e:
            error_msg = f"❌ Критическая ошибка: {str(e)}"
            await message.reply(error_msg)
            logger.error(f"Error in handle_object: {e}", exc_info=True)

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
        # Получаем язык админа из БД
        with Session() as session:
            admin_user = session.query(User).filter_by(telegramID=message.from_user.id).first()
            admin_lang = admin_user.lang if admin_user else 'en'

        reply = await message.reply("🔄 Loading...")

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
                    await self.message_manager.send_template(
                        user=admin_user,
                        template_key='admin/testmail/invalid_provider',
                        variables={'provider': forced_provider},
                        update=reply,
                        edit=True
                    )
                    return

            # Validate custom email if provided
            if custom_email:
                import re
                if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', custom_email):
                    await self.message_manager.send_template(
                        user=admin_user,
                        template_key='admin/testmail/invalid_email',
                        variables={'email': custom_email},
                        update=reply,
                        edit=True
                    )
                    return

            # Check providers configuration
            from email_sender import email_manager

            if not email_manager.providers:
                await self.message_manager.send_template(
                    user=admin_user,
                    template_key='admin/testmail/no_providers',
                    update=reply,
                    edit=True
                )
                return

            # Test all providers
            await self.message_manager.send_template(
                user=admin_user,
                template_key='admin/testmail/checking',
                update=reply,
                edit=True
            )

            providers_status = await email_manager.get_providers_status()

            # Build status report using modular templates
            template_keys = ['admin/testmail/header']
            working_providers = []

            for provider_name, is_working in providers_status.items():
                if provider_name == 'smtp':
                    template_keys.append('admin/testmail/status_smtp')
                elif provider_name == 'mailgun':
                    template_keys.append('admin/testmail/status_mailgun')

                if is_working:
                    working_providers.append(provider_name)

            # Add secure domains info
            if email_manager.secure_domains:
                template_keys.append('admin/testmail/secure_domains')
            else:
                template_keys.append('admin/testmail/no_secure_domains')

            # Determine target email
            with Session() as session:
                admin_user_db = session.query(User).filter_by(telegramID=message.from_user.id).first()

                if custom_email:
                    target_email = custom_email
                    # Если админ тестирует свой собственный email - используем его имя
                    if admin_user_db and admin_user_db.email == custom_email:
                        firstname = admin_user_db.firstname
                    else:
                        firstname = "Test User"

                else:
                    user = session.query(User).filter_by(telegramID=message.from_user.id).first()
                    if not user or not user.email:
                        template_keys.append('admin/testmail/no_user_email')

                        await self.message_manager.send_template(
                            user=admin_user,
                            template_key=template_keys,
                            variables={
                                'smtp_host': config.SMTP_HOST,
                                'smtp_port': config.SMTP_PORT,
                                'smtp_status': '✅ OK' if providers_status.get('smtp', False) else '❌ FAIL',
                                'mailgun_domain': config.MAILGUN_DOMAIN,
                                'mailgun_region': config.MAILGUN_REGION,
                                'mailgun_status': '✅ OK' if providers_status.get('mailgun', False) else '❌ FAIL',
                                'domains': ', '.join(email_manager.secure_domains)
                            },
                            update=reply,
                            edit=True
                        )
                        return
                    target_email = user.email
                    firstname = user.firstname

            # Determine which provider will be used
            if forced_provider:
                if forced_provider not in working_providers:
                    template_keys.append('admin/testmail/provider_not_working')
                    await self.message_manager.send_template(
                        user=admin_user,
                        template_key=template_keys,
                        variables={
                            'smtp_host': config.SMTP_HOST,
                            'smtp_port': config.SMTP_PORT,
                            'smtp_status': '✅ OK' if providers_status.get('smtp', False) else '❌ FAIL',
                            'mailgun_domain': config.MAILGUN_DOMAIN,
                            'mailgun_region': config.MAILGUN_REGION,
                            'mailgun_status': '✅ OK' if providers_status.get('mailgun', False) else '❌ FAIL',
                            'domains': ', '.join(email_manager.secure_domains) if email_manager.secure_domains else '',
                            'provider': forced_provider.upper()
                        },
                        update=reply,
                        edit=True
                    )
                    return
                selected_provider = forced_provider
                template_keys.append('admin/testmail/reason_forced')
            else:
                provider_order = email_manager._select_provider_for_email(target_email)
                if not provider_order:
                    template_keys.append('admin/testmail/no_available_providers')
                    # ... отправка с ошибкой
                    return

                selected_provider = provider_order[0]
                domain = email_manager._get_email_domain(target_email)

                if domain in email_manager.secure_domains:
                    template_keys.append('admin/testmail/reason_secure')
                else:
                    template_keys.append('admin/testmail/reason_regular')

            # Add sending status
            template_keys.append('admin/testmail/sending')

            # Send status message
            await self.message_manager.send_template(
                user=admin_user,
                template_key=template_keys,
                variables={
                    'smtp_host': config.SMTP_HOST,
                    'smtp_port': config.SMTP_PORT,
                    'smtp_status': '✅ OK' if providers_status.get('smtp', False) else '❌ FAIL',
                    'mailgun_domain': config.MAILGUN_DOMAIN,
                    'mailgun_region': config.MAILGUN_REGION,
                    'mailgun_status': '✅ OK' if providers_status.get('mailgun', False) else '❌ FAIL',
                    'domains': ', '.join(email_manager.secure_domains) if email_manager.secure_domains else '',
                    'target_email': target_email,
                    'provider': selected_provider.upper(),
                    'domain': email_manager._get_email_domain(target_email)
                },
                update=reply,
                edit=True
            )

            # Get email body template
            email_subject, _ = await MessageTemplates.get_raw_template(
                'admin/testmail/email_subject',
                {'provider': selected_provider.upper()},
                lang=admin_lang
            )

            email_body, _ = await MessageTemplates.get_raw_template(
                'admin/testmail/email_body',
                {
                    'firstname': firstname,
                    'target_email': target_email,
                    'provider': selected_provider.upper(),
                    'time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                },
                lang=admin_lang
            )

            # Send test email
            provider = email_manager.providers[selected_provider]
            success = await provider.send_email(
                to=target_email,
                subject=email_subject,
                html_body=email_body,
                text_body=None
            )

            # Build final status message
            if success:
                final_templates = ['admin/testmail/header']

                # Add provider statuses
                for provider_name in providers_status.keys():
                    if provider_name == 'smtp':
                        final_templates.append('admin/testmail/status_smtp')
                    elif provider_name == 'mailgun':
                        final_templates.append('admin/testmail/status_mailgun')

                # Add secure domains
                if email_manager.secure_domains:
                    final_templates.append('admin/testmail/secure_domains')
                else:
                    final_templates.append('admin/testmail/no_secure_domains')

                # Add success message
                final_templates.append('admin/testmail/success')

                # Add fallback info if applicable
                fallback_provider = ''
                if not forced_provider:
                    provider_order = email_manager._select_provider_for_email(target_email)
                    if len(provider_order) > 1:
                        final_templates.append('admin/testmail/fallback')
                        fallback_provider = provider_order[1].upper()

                await self.message_manager.send_template(
                    user=admin_user,
                    template_key=final_templates,
                    variables={
                        'smtp_host': config.SMTP_HOST,
                        'smtp_port': config.SMTP_PORT,
                        'smtp_status': '✅ OK' if providers_status.get('smtp', False) else '❌ FAIL',
                        'mailgun_domain': config.MAILGUN_DOMAIN,
                        'mailgun_region': config.MAILGUN_REGION,
                        'mailgun_status': '✅ OK' if providers_status.get('mailgun', False) else '❌ FAIL',
                        'domains': ', '.join(email_manager.secure_domains) if email_manager.secure_domains else '',
                        'target_email': target_email,
                        'provider': selected_provider.upper(),
                        'fallback_provider': fallback_provider
                    },
                    update=reply,
                    edit=True
                )
            else:
                # Error message
                error_templates = ['admin/testmail/header']
                # ... добавляем шаблоны для ошибки
                error_templates.append('admin/testmail/send_error')

                await self.message_manager.send_template(
                    user=admin_user,
                    template_key=error_templates,
                    variables={
                        'smtp_host': config.SMTP_HOST,
                        'smtp_port': config.SMTP_PORT,
                        'smtp_status': '✅ OK' if providers_status.get('smtp', False) else '❌ FAIL',
                        'mailgun_domain': config.MAILGUN_DOMAIN,
                        'mailgun_region': config.MAILGUN_REGION,
                        'mailgun_status': '✅ OK' if providers_status.get('mailgun', False) else '❌ FAIL',
                        'domains': ', '.join(email_manager.secure_domains) if email_manager.secure_domains else '',
                        'target_email': target_email,
                        'provider': selected_provider.upper()
                    },
                    update=reply,
                    edit=True
                )

        except Exception as e:
            await message.reply(f"❌ Critical error: {str(e)}")
            logger.error(f"Error in testmail command: {e}", exc_info=True)

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
                    f"📦 Project: {purchase.projectName} (ID: {purchase.projectID})\n"
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

        # ОСНОВНЫЕ КОМАНДЫ
        if command == "import":
            await self.handle_import(message)

        elif command == "restore":
            await self.handle_restore(message)

        elif command == "object":
            await self.handle_object(message)

        elif command == "upconfig":
            await self.handle_upconfig(message)

        elif command == "upro":
            # Обновление Projects и Options
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
            # Обновление шаблонов
            try:
                reply = await message.reply("🔄 Обновляю шаблоны...")
                await MessageTemplates.load_templates()
                await reply.edit_text("✅ Шаблоны успешно обновлены")
            except Exception as e:
                error_msg = f"❌ Ошибка обновления шаблонов: {str(e)}"
                logger.error(error_msg, exc_info=True)
                await message.reply(error_msg)

        elif command.startswith("delpurchase"):
            await self.handle_delpurchase(message)

        elif command == "testmail":
            await self.handle_testmail(message)

        elif command == "legacy":

            try:
                reply = await message.reply("🔄 Запускаю legacy миграцию...")
                from legacy_user_processor import legacy_processor
                try:
                    # This will raise RuntimeError if migration is already running
                    stats = await legacy_processor._process_legacy_users()

                    # Build detailed report
                    report = f"📊 Legacy Migration Report:\n\n"
                    report += f"📋 Total records: {stats.total_records}\n"
                    report += f"👤 Users found: {stats.users_found}\n"
                    report += f"👥 Upliners assigned: {stats.upliners_assigned}\n"
                    report += f"📈 Purchases created: {stats.purchases_created}\n"
                    report += f"✅ Completed: {stats.completed}\n"
                    report += f"❌ Errors: {stats.errors}\n"

                    # Add summary based on results
                    if stats.users_found == 0 and stats.upliners_assigned == 0 and stats.purchases_created == 0:
                        report += "\n🔍 No new legacy users found to process."
                    else:
                        report += "\n🎯 Legacy migration processing completed!"

                    # Add error details if any
                    if stats.errors > 0 and stats.error_details:
                        report += "\n\n⚠️ Error details (first 10):\n"
                        for email, error in stats.error_details[:10]:
                            report += f"• {email}: {error}\n"
                    await reply.edit_text(report)


                except RuntimeError as e:
                    if "already in progress" in str(e):
                        await reply.edit_text(
                            "⚠️ Автоматическая миграция идёт прямо сейчас.\n"
                            "Читайте логи для отслеживания прогресса.\n\n"
                            "Миграция запускается автоматически каждые 10 минут."
                        )

                    else:
                        raise
            except Exception as e:
                error_msg = f"❌ Ошибка при legacy миграции: {str(e)}"
                logger.error(error_msg, exc_info=True)
                await message.reply(error_msg)


def setup_admin_commands(dp, message_manager):
    dp.filters_factory.bind(AdminFilter)
    admin_commands = AdminCommands(dp, message_manager)  # Передаем message_manager

    # Регистрируем middleware
    admin_middleware = AdminCommandsMiddleware(admin_commands)
    dp.middleware.setup(admin_middleware)

    logger.info("Admin commands initialized with middleware")