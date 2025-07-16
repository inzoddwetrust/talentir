import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from google_services import get_google_services
from database import User, Project, Purchase, ActiveBalance, Notification
from init import Session
from templates import MessageTemplates
import helpers
import config

logger = logging.getLogger(__name__)


@dataclass
class LegacyUserRecord:
    """Запись о legacy пользователе из Google Sheets"""
    row_index: int
    email: str
    upliner_email: str
    project_name: str
    qty: int
    is_found: str
    upliner_found: str
    purchase: str
    done: str


class LegacyUserProcessor:
    """Процессор для миграции пользователей со старой платформы"""

    def __init__(self, check_interval: int = 600):  # 10 минут
        self.check_interval = check_interval
        self._running = False

    async def start(self):
        """Запуск фонового процесса миграции"""
        if self._running:
            logger.warning("Legacy user processor is already running")
            return

        self._running = True
        logger.info("Starting legacy user migration processor")
        await self._run_migration_loop()

    async def stop(self):
        """Остановка процесса миграции"""
        self._running = False
        logger.info("Stopping legacy user migration processor")

    async def _run_migration_loop(self):
        """Основной цикл миграции"""
        while self._running:
            try:
                await self._process_legacy_users()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Error in legacy migration loop: {e}", exc_info=True)
                await asyncio.sleep(self.check_interval)

    async def _get_legacy_users(self) -> List[LegacyUserRecord]:
        """Получает данные legacy пользователей из Google Sheets"""
        try:
            sheets_client, _ = get_google_services()
            sheet = sheets_client.open_by_key(config.LEGACY_SHEET_ID).worksheet("Users")

            # Получаем все записи
            records = sheet.get_all_records()

            legacy_users = []
            for idx, record in enumerate(records, start=2):  # row 2 is first data row
                try:
                    # Проверяем обязательные поля
                    if not record.get('Email') or not record.get('Project') or not record.get('Qty'):
                        continue

                    legacy_user = LegacyUserRecord(
                        row_index=idx,
                        email=record['Email'].strip().lower(),
                        upliner_email=record.get('Upliner', '').strip().lower(),
                        project_name=record['Project'].strip(),
                        qty=int(record['Qty']),
                        is_found=str(record.get('IsFound', '')).strip(),
                        upliner_found=str(record.get('UplinerFound', '')).strip(),
                        purchase=str(record.get('Purchase', '')).strip(),
                        done=str(record.get('Done', '')).strip()
                    )

                    # Проверяем дубликаты
                    if any(lu.email == legacy_user.email for lu in legacy_users):
                        logger.warning(f"Duplicate email found in legacy sheet: {legacy_user.email}")
                        continue

                    legacy_users.append(legacy_user)

                except (ValueError, TypeError) as e:
                    logger.error(f"Error parsing legacy user at row {idx}: {e}")
                    continue

            logger.info(f"Loaded {len(legacy_users)} legacy users from Google Sheets")
            return legacy_users

        except Exception as e:
            logger.error(f"Error loading legacy users: {e}", exc_info=True)
            return []

    async def _process_legacy_users(self):
        """Обрабатывает всех legacy пользователей"""
        legacy_users = await self._get_legacy_users()

        if not legacy_users:
            return

        with Session() as session:
            updates_made = False

            for legacy_user in legacy_users:
                try:
                    # Этап 1: Поиск пользователя
                    if not legacy_user.is_found:
                        if await self._find_and_mark_user(session, legacy_user):
                            updates_made = True

                    # Этап 2: Поиск аплайнера
                    if legacy_user.is_found and not legacy_user.upliner_found:
                        if await self._find_and_assign_upliner(session, legacy_user):
                            updates_made = True

                    # Этап 3: Начисление акций
                    if legacy_user.is_found and not legacy_user.purchase:
                        if await self._create_legacy_purchase(session, legacy_user):
                            updates_made = True

                    # Этап 4: Финализация
                    if (legacy_user.is_found and legacy_user.purchase and
                            not legacy_user.done):
                        if await self._finalize_legacy_user(session, legacy_user):
                            updates_made = True

                except Exception as e:
                    logger.error(f"Error processing legacy user {legacy_user.email}: {e}")
                    continue

            if updates_made:
                logger.info("Legacy migration updates completed")

    async def _find_and_mark_user(self, session: Session, legacy_user: LegacyUserRecord) -> bool:
        """Находит пользователя в системе и отмечает его как найденного"""
        try:
            # Ищем пользователя с верифицированным email
            db_user = session.query(User).filter_by(email=legacy_user.email).first()

            if not db_user:
                return False

            # Проверяем что email верифицирован
            email_confirmed = helpers.get_user_note(db_user, 'emailConfirmed')
            if email_confirmed != '1':
                return False

            # Обновляем Google Sheets
            await self._update_sheet_field(legacy_user.row_index, 'IsFound', str(db_user.userID))

            # Отправляем уведомление пользователю
            await self._send_legacy_welcome_notification(db_user, legacy_user)

            logger.info(f"Found legacy user: {legacy_user.email} -> UserID {db_user.userID}")
            return True

        except Exception as e:
            logger.error(f"Error finding user {legacy_user.email}: {e}")
            return False

    async def _find_and_assign_upliner(self, session: Session, legacy_user: LegacyUserRecord) -> bool:
        """Находит и назначает аплайнера пользователю"""
        try:
            if not legacy_user.upliner_email:
                return False

            # Ищем аплайнера
            upliner = session.query(User).filter_by(email=legacy_user.upliner_email).first()

            if not upliner:
                return False

            # Проверяем что email аплайнера верифицирован
            email_confirmed = helpers.get_user_note(upliner, 'emailConfirmed')
            if email_confirmed != '1':
                return False

            # Получаем пользователя
            user_id = int(legacy_user.is_found)
            db_user = session.query(User).filter_by(userID=user_id).first()

            if not db_user:
                logger.error(f"User {user_id} not found when assigning upliner")
                return False

            # Обновляем аплайнера
            old_upline = db_user.upline
            db_user.upline = upliner.telegramID
            session.commit()

            # Обновляем Google Sheets
            await self._update_sheet_field(legacy_user.row_index, 'UplinerFound', str(upliner.userID))

            # Отправляем уведомления
            await self._send_upliner_assigned_notifications(db_user, upliner, old_upline)

            logger.info(f"Assigned upliner {upliner.email} to user {db_user.email}")
            return True

        except Exception as e:
            logger.error(f"Error assigning upliner for {legacy_user.email}: {e}")
            return False

    async def _create_legacy_purchase(self, session: Session, legacy_user: LegacyUserRecord) -> bool:
        """Создает покупку для legacy пользователя"""
        try:
            # Получаем пользователя
            user_id = int(legacy_user.is_found)
            db_user = session.query(User).filter_by(userID=user_id).first()

            if not db_user:
                logger.error(f"User {user_id} not found when creating purchase")
                return False

            # Ищем проект
            project = session.query(Project).filter_by(projectName=legacy_user.project_name).first()

            if not project:
                logger.error(f"Project {legacy_user.project_name} not found for legacy user {legacy_user.email}")
                return False

            # Создаем покупку (без списания баланса)
            purchase = Purchase(
                userID=db_user.userID,
                projectID=project.projectID,
                projectName=project.projectName,
                optionID=None,  # Legacy purchase, no specific option
                packQty=legacy_user.qty,
                packPrice=0.0,  # Free legacy shares
                createdAt=datetime.utcnow()
            )

            session.add(purchase)
            session.flush()

            # Добавляем запись в ActiveBalance для истории
            balance_record = ActiveBalance(
                userID=db_user.userID,
                firstname=db_user.firstname,
                surname=db_user.surname,
                amount=0.0,
                status='done',
                reason=f'legacy_migration={purchase.purchaseID}',
                notes=f'Legacy shares migration: {legacy_user.qty} shares of {project.projectName}'
            )
            session.add(balance_record)
            session.commit()

            # Обновляем Google Sheets
            await self._update_sheet_field(legacy_user.row_index, 'Purchase', str(purchase.purchaseID))

            # Отправляем уведомления
            await self._send_legacy_purchase_notifications(db_user, purchase, legacy_user)

            logger.info(f"Created legacy purchase {purchase.purchaseID} for user {db_user.email}")
            return True

        except Exception as e:
            logger.error(f"Error creating legacy purchase for {legacy_user.email}: {e}")
            return False

    async def _finalize_legacy_user(self, session: Session, legacy_user: LegacyUserRecord) -> bool:
        """Финализирует обработку legacy пользователя"""
        try:
            # Отмечаем как завершенного
            await self._update_sheet_field(legacy_user.row_index, 'Done', 'YES')

            logger.info(f"Finalized legacy user: {legacy_user.email}")
            return True

        except Exception as e:
            logger.error(f"Error finalizing legacy user {legacy_user.email}: {e}")
            return False

    async def _update_sheet_field(self, row_index: int, field_name: str, value: str):
        """Обновляет поле в Google Sheets"""
        try:
            sheets_client, _ = get_google_services()
            sheet = sheets_client.open_by_key(config.LEGACY_SHEET_ID).worksheet("Users")

            # Определяем колонку по имени поля
            field_columns = {
                'IsFound': 'E',
                'UplinerFound': 'F',
                'Purchase': 'G',
                'Done': 'H'
            }

            if field_name not in field_columns:
                logger.error(f"Unknown field name: {field_name}")
                return

            cell_address = f"{field_columns[field_name]}{row_index}"
            sheet.update(cell_address, value)

            logger.debug(f"Updated sheet cell {cell_address} = {value}")

        except Exception as e:
            logger.error(f"Error updating sheet field {field_name}: {e}")

    async def _send_legacy_welcome_notification(self, user: User, legacy_user: LegacyUserRecord):
        """Отправляет уведомление о найденном legacy пользователе"""
        try:
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_user_welcome',
                {
                    'firstname': user.firstname,
                    'project_name': legacy_user.project_name,
                    'qty': legacy_user.qty
                },
                lang=user.lang
            )

            notification = Notification(
                source="legacy_migration",
                text=text,
                buttons=buttons,
                target_type="user",
                target_value=str(user.userID),
                priority=2,
                category="legacy",
                importance="high",
                parse_mode="HTML"
            )

            with Session() as session:
                session.add(notification)
                session.commit()

        except Exception as e:
            logger.error(f"Error sending legacy welcome notification: {e}")

    async def _send_upliner_assigned_notifications(self, user: User, upliner: User, old_upline: int):
        """Отправляет уведомления о назначении аплайнера"""
        try:
            # Уведомление пользователю
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_upliner_assigned_user',
                {
                    'firstname': user.firstname,
                    'upliner_name': upliner.firstname
                },
                lang=user.lang
            )

            user_notification = Notification(
                source="legacy_migration",
                text=text,
                buttons=buttons,
                target_type="user",
                target_value=str(user.userID),
                priority=2,
                category="legacy",
                importance="normal",
                parse_mode="HTML"
            )

            # Уведомление аплайнеру
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_upliner_assigned_upliner',
                {
                    'firstname': upliner.firstname,
                    'user_name': user.firstname
                },
                lang=upliner.lang
            )

            upliner_notification = Notification(
                source="legacy_migration",
                text=text,
                buttons=buttons,
                target_type="user",
                target_value=str(upliner.userID),
                priority=2,
                category="legacy",
                importance="normal",
                parse_mode="HTML"
            )

            # Уведомление админам
            admin_text, admin_buttons = await MessageTemplates.get_raw_template(
                'legacy_upliner_assigned_admin',
                {
                    'user_name': user.firstname,
                    'user_id': user.userID,
                    'upliner_name': upliner.firstname,
                    'upliner_id': upliner.userID,
                    'old_upline': old_upline
                }
            )

            for admin_id in config.ADMIN_USER_IDS:
                admin_notification = Notification(
                    source="legacy_migration",
                    text=admin_text,
                    buttons=admin_buttons,
                    target_type="user",
                    target_value=str(admin_id),
                    priority=1,
                    category="legacy",
                    importance="normal",
                    parse_mode="HTML"
                )

                with Session() as session:
                    session.add(admin_notification)

            with Session() as session:
                session.add(user_notification)
                session.add(upliner_notification)
                session.commit()

        except Exception as e:
            logger.error(f"Error sending upliner assigned notifications: {e}")

    async def _send_legacy_purchase_notifications(self, user: User, purchase: Purchase, legacy_user: LegacyUserRecord):
        """Отправляет уведомления о начислении legacy акций"""
        try:
            # Уведомление пользователю
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_purchase_created_user',
                {
                    'firstname': user.firstname,
                    'qty': legacy_user.qty,
                    'project_name': legacy_user.project_name,
                    'purchase_id': purchase.purchaseID
                },
                lang=user.lang
            )

            user_notification = Notification(
                source="legacy_migration",
                text=text,
                buttons=buttons,
                target_type="user",
                target_value=str(user.userID),
                priority=2,
                category="legacy",
                importance="high",
                parse_mode="HTML"
            )

            # Уведомление админам
            admin_text, admin_buttons = await MessageTemplates.get_raw_template(
                'legacy_purchase_created_admin',
                {
                    'user_name': user.firstname,
                    'user_id': user.userID,
                    'user_email': user.email,
                    'qty': legacy_user.qty,
                    'project_name': legacy_user.project_name,
                    'purchase_id': purchase.purchaseID
                }
            )

            for admin_id in config.ADMIN_USER_IDS:
                admin_notification = Notification(
                    source="legacy_migration",
                    text=admin_text,
                    buttons=admin_buttons,
                    target_type="user",
                    target_value=str(admin_id),
                    priority=1,
                    category="legacy",
                    importance="normal",
                    parse_mode="HTML"
                )

                with Session() as session:
                    session.add(admin_notification)

            with Session() as session:
                session.add(user_notification)
                session.commit()

        except Exception as e:
            logger.error(f"Error sending legacy purchase notifications: {e}")


# Singleton instance
legacy_processor = LegacyUserProcessor()