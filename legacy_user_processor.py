import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from google_services import get_google_services
from database import User, Project, Purchase, ActiveBalance, Notification, Option
from init import Session
from templates import MessageTemplates
import helpers
import config

logger = logging.getLogger(__name__)

LEGACY_SHEET_ID = "1mbaRSbOs0Hc98iJ3YnZnyqL5yxeSuPJCef5PFjPHpFg"


class MigrationStatus(Enum):
    PENDING = "pending"
    USER_FOUND = "user_found"
    UPLINER_ASSIGNED = "upliner_assigned"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class LegacyUserRecord:
    row_index: int
    email: str
    upliner: str
    project: str
    qty: int
    is_found: str
    upliner_found: str
    purchase_done: str
    error_count: int = 0
    last_error: str = ""

    @property
    def status(self) -> MigrationStatus:
        # Check if user found (either old format "1" or new format - userID)
        user_found = self.is_found and self.is_found != "" and self.is_found != "0"

        if user_found and self.upliner_found == "1" and self.purchase_done == "1":
            return MigrationStatus.COMPLETED
        elif self.error_count > 3:
            return MigrationStatus.ERROR
        else:
            return MigrationStatus.PENDING


@dataclass
class MigrationStats:
    total_records: int = 0
    users_found: int = 0
    upliners_assigned: int = 0
    purchases_created: int = 0
    completed: int = 0
    errors: int = 0
    error_details: List[Tuple[str, str]] = field(default_factory=list)

    def add_error(self, email: str, error: str):
        self.errors += 1
        self.error_details.append((email, error))
        logger.error(f"Migration error for {email}: {error}")


class LegacyUserProcessor:
    def __init__(self, check_interval: int = 600, batch_size: int = 50):
        """
        Initialize legacy user processor.

        Args:
            check_interval: Seconds between automatic processing runs (600 = 10 minutes)
            batch_size: Number of records to process in one batch (increased from 10 to 50)
        """
        self.check_interval = check_interval
        self.batch_size = batch_size
        self._running = False
        self._processing = False  # Flag for active processing (for lock)
        self._cache = None  # Cache for Google Sheets data
        self._cache_loaded_at = None  # Timestamp of last cache load

    @staticmethod
    def normalize_email(email: str) -> str:
        """
        Universal email normalization for case-insensitive search.
        For Gmail also removes dots in local part.
        """
        if not email:
            return ""

        email = email.lower().strip()

        # Special handling for Gmail
        if '@gmail.com' in email:
            local, domain = email.split('@', 1)
            local = local.replace('.', '')  # Remove dots
            return f"{local}@{domain}"

        return email

    async def start(self):
        if self._running:
            logger.warning("Legacy processor already running")
            return
        self._running = True
        logger.info("Starting legacy migration processor")
        await self._run_migration_loop()

    async def stop(self):
        self._running = False
        logger.info("Stopping legacy migration processor")

    async def _run_migration_loop(self):
        consecutive_errors = 0
        max_consecutive_errors = 5

        while self._running:
            try:
                stats = await self._process_legacy_users()

                if any([stats.users_found, stats.upliners_assigned, stats.purchases_created]):
                    logger.info(
                        f"Migration progress: found={stats.users_found}, upliners={stats.upliners_assigned}, purchases={stats.purchases_created}")

                if stats.errors > 0:
                    consecutive_errors += 1
                    sleep_time = min(self.check_interval * (2 ** consecutive_errors), 3600)
                else:
                    consecutive_errors = 0
                    sleep_time = self.check_interval

                if consecutive_errors >= max_consecutive_errors:
                    logger.error("Too many consecutive errors, stopping")
                    break

                await asyncio.sleep(sleep_time)

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Critical error in migration loop: {e}", exc_info=True)
                if consecutive_errors >= max_consecutive_errors:
                    break
                await asyncio.sleep(self.check_interval * consecutive_errors)

    async def _load_cache(self, force: bool = False):
        """
        Load all records from Google Sheets into memory cache.

        Args:
            force: Force reload even if cache exists
        """
        if not force and self._cache is not None:
            logger.debug("Using existing cache")
            return

        try:
            logger.info("Loading legacy users from Google Sheets to cache...")
            sheets_client, _ = get_google_services()
            sheet = sheets_client.open_by_key(LEGACY_SHEET_ID).worksheet("Users")
            records = sheet.get_all_records()

            self._cache = records
            self._cache_loaded_at = datetime.now()
            logger.info(f"Cache loaded: {len(records)} records at {self._cache_loaded_at}")

        except Exception as e:
            logger.error(f"Failed to load cache from Google Sheets: {e}", exc_info=True)
            raise

    async def _get_legacy_users(self) -> List[LegacyUserRecord]:
        """
        Load and parse legacy users from cached Google Sheets data.
        No duplicate filtering - each row is processed independently.
        """
        try:
            # Use cached data instead of loading from sheets
            if self._cache is None:
                await self._load_cache()

            records = self._cache

            if not records:
                logger.warning("No records found in cache")
                return []

            logger.info(f"Processing {len(records)} cached records")

            legacy_users = []

            for idx, record in enumerate(records, start=2):
                try:
                    # Validation
                    required_fields = ['email', 'project', 'qty']
                    if not all(record.get(field) for field in required_fields):
                        continue

                    email = record['email'].strip().lower()

                    # NO duplicate check - each row is independent

                    if '@' not in email or '.' not in email:
                        continue

                    try:
                        qty = int(record['qty'])
                        if qty <= 0:
                            continue
                    except (ValueError, TypeError):
                        continue

                    legacy_user = LegacyUserRecord(
                        row_index=idx,
                        email=email,
                        upliner=record.get('upliner', '').strip(),
                        project=record['project'].strip(),
                        qty=qty,
                        is_found=str(record.get('IsFound', '')).strip(),
                        upliner_found=str(record.get('UplinerFound', '')).strip(),
                        purchase_done=str(record.get('PurchaseDone', '')).strip()
                    )

                    legacy_users.append(legacy_user)

                except Exception as e:
                    logger.error(f"Error parsing row {idx}: {e}")
                    continue

            logger.info(f"Loaded {len(legacy_users)} valid users from cache")

            # Status distribution
            status_counts = {}
            for user in legacy_users:
                status = user.status
                status_counts[status] = status_counts.get(status, 0) + 1
            logger.info(f"Status distribution: {dict(status_counts)}")

            return legacy_users

        except Exception as e:
            logger.error(f"Error loading legacy users from cache: {e}", exc_info=True)
            return []

    async def _process_legacy_users(self) -> MigrationStats:
        """
        Process all pending legacy users from cached data.
        Implements locking to prevent concurrent execution.
        Reloads cache on each run for fresh data.
        """
        # Check if processing is already running
        if self._processing:
            raise RuntimeError("Migration already in progress")

        self._processing = True
        try:
            # Force reload cache at the start of each processing run
            await self._load_cache(force=True)

            legacy_users = await self._get_legacy_users()
            stats = MigrationStats(total_records=len(legacy_users))

            if not legacy_users:
                return stats

            # Filter pending users (not all three flags are set)
            pending_users = [
                user for user in legacy_users
                if user.status != MigrationStatus.COMPLETED and user.status != MigrationStatus.ERROR
            ]

            if not pending_users:
                logger.info("No pending users to process")
                return stats

            logger.info(f"Processing {len(pending_users)} pending users")

            # Process in batches
            for i in range(0, len(pending_users), self.batch_size):
                batch = pending_users[i:i + self.batch_size]
                logger.debug(
                    f"Processing batch {i // self.batch_size + 1} of {(len(pending_users) - 1) // self.batch_size + 1}")

                with Session() as session:
                    for legacy_user in batch:
                        try:
                            # Create copy of state before processing
                            before_state = (legacy_user.is_found, legacy_user.upliner_found, legacy_user.purchase_done)

                            progress_made = await self._process_single_user(session, legacy_user)

                            # Check changes and update statistics
                            if progress_made:
                                after_state = (legacy_user.is_found, legacy_user.upliner_found,
                                               legacy_user.purchase_done)

                                # User found (transition from empty/0 to userID)
                                if not before_state[0] and after_state[0]:
                                    stats.users_found += 1

                                # Upliner assigned
                                if before_state[1] != "1" and after_state[1] == "1":
                                    stats.upliners_assigned += 1

                                # Purchase created
                                if before_state[2] != "1" and after_state[2] == "1":
                                    stats.purchases_created += 1

                                # Check if completed
                                if legacy_user.status == MigrationStatus.COMPLETED:
                                    stats.completed += 1

                        except Exception as e:
                            stats.add_error(legacy_user.email, str(e))
                            logger.error(f"Error processing {legacy_user.email} row {legacy_user.row_index}: {e}")

                # Small delay between batches to avoid overload
                await asyncio.sleep(0.1)

            logger.info(
                f"Migration batch completed: found={stats.users_found}, "
                f"upliners={stats.upliners_assigned}, purchases={stats.purchases_created}, "
                f"errors={stats.errors}"
            )
            return stats

        finally:
            # Always release the lock
            self._processing = False
            logger.debug("Processing lock released")

    def _get_user_from_legacy_record(self, session: Session, user: LegacyUserRecord) -> Optional[User]:
        """
        Get user from DB by legacy record.
        Supports both old format (is_found="1") and new format (is_found=userID).
        With email normalization for case-insensitive search.
        """
        if not user.is_found or user.is_found in ["", "0"]:
            return None

        # New format: is_found contains userID
        if user.is_found != "1":
            try:
                user_id = int(user.is_found)
                return session.query(User).filter_by(userID=user_id).first()
            except (ValueError, TypeError):
                logger.warning(f"Invalid userID format in is_found: {user.is_found}")
                return None

        # Old format: is_found="1", search by email with normalization
        normalized_search_email = self.normalize_email(user.email)
        users_with_email = session.query(User).filter(User.email.isnot(None)).all()

        for u in users_with_email:
            if self.normalize_email(u.email) == normalized_search_email:
                return u

        return None

    async def _process_single_user(self, session: Session, user: LegacyUserRecord) -> bool:
        progress_made = False

        try:
            # INDEPENDENT CHECK 1: IsFound
            if not user.is_found or user.is_found in ["", "0"]:
                if await self._find_user(session, user):
                    progress_made = True

            # INDEPENDENT CHECK 2: PurchaseDone (only if user found)
            if user.is_found and user.is_found not in ["", "0"] and user.purchase_done != "1":
                if await self._create_purchase(session, user):
                    user.purchase_done = "1"
                    progress_made = True

            # INDEPENDENT CHECK 3: UplinerFound (only if user found and has upliner)
            if (user.is_found and user.is_found not in ["", "0"] and
                    user.upliner and user.upliner_found != "1"):
                if await self._assign_upliner(session, user):
                    user.upliner_found = "1"
                    progress_made = True

            return progress_made

        except Exception as e:
            # Only real technical errors increase the counter
            user.error_count += 1
            user.last_error = str(e)
            logger.error(f"Technical error processing {user.email}: {e}", exc_info=True)
            return False

    async def _find_user(self, session: Session, user: LegacyUserRecord) -> bool:
        try:
            # Email normalization for search (case-insensitive)
            normalized_search_email = self.normalize_email(user.email)

            # Search user with normalized email
            users_with_email = session.query(User).filter(User.email.isnot(None)).all()
            db_user = None

            for u in users_with_email:
                if self.normalize_email(u.email) == normalized_search_email:
                    db_user = u
                    break

            if not db_user:
                logger.debug(f"User {user.email} not found in database")
                return False

            email_confirmed = helpers.get_user_note(db_user, 'emailConfirmed')
            if email_confirmed != '1':
                logger.debug(f"User {user.email} email not confirmed yet")
                return False

            # IMPROVEMENT: write userID instead of "1"
            await self._update_sheet(user.row_index, 'IsFound', str(db_user.userID))
            user.is_found = str(db_user.userID)  # Update local copy

            await self._send_welcome_notification(db_user, user)

            logger.info(f"Found legacy user: {user.email} -> UserID {db_user.userID}")
            return True

        except Exception as e:
            logger.error(f"Error finding user {user.email}: {e}")
            return False

    async def _assign_upliner(self, session: Session, user: LegacyUserRecord) -> bool:
        """
        Assign upliner to the user based on legacy record.
        Supports "SAME" keyword to keep existing upliner.
        Empty upliner field is an error.
        """
        try:
            # Get user from DB
            db_user = self._get_user_from_legacy_record(session, user)
            if not db_user:
                logger.debug(f"User {user.email} not found yet")
                return False

            # CRITICAL: Empty upliner is an error - operator must think!
            if not user.upliner:
                logger.error(f"Empty upliner for {user.email} at row {user.row_index} - skipping")
                return False

            # Handle "SAME" keyword - keep existing upliner
            if user.upliner.upper() == "SAME":
                logger.info(f"Keeping existing upliner {db_user.upline} for {user.email} (row {user.row_index})")
                await self._update_sheet(user.row_index, 'UplinerFound', '1')
                return True

            # Find upliner by email with normalization
            normalized_upliner_email = self.normalize_email(user.upliner)

            # Search for upliner with case-insensitive email
            users_with_email = session.query(User).filter(User.email.isnot(None)).all()
            upliner = None
            for u in users_with_email:
                if self.normalize_email(u.email) == normalized_upliner_email:
                    upliner = u
                    break

            if not upliner:
                logger.debug(f"Upliner {user.upliner} not found yet")
                return False

            # Check if upliner's email is confirmed
            email_confirmed = helpers.get_user_note(upliner, 'emailConfirmed')
            if email_confirmed != '1':
                logger.debug(f"Upliner {user.upliner} email not confirmed yet")
                return False

            old_upline = db_user.upline

            # LEGACY MIGRATION: FORCEFULLY set upliner from table
            # Legacy data is the truth, it overwrites any existing upliner
            if old_upline != upliner.telegramID:
                if old_upline and old_upline != config.DEFAULT_REFERRER_ID:
                    logger.info(
                        f"LEGACY: Changing upliner for {user.email} (row {user.row_index}) "
                        f"from {old_upline} to {upliner.telegramID}"
                    )
                else:
                    logger.info(
                        f"LEGACY: Setting upliner for {user.email} (row {user.row_index}) "
                        f"to {upliner.telegramID}"
                    )

                db_user.upline = upliner.telegramID
                session.commit()
                await self._send_upliner_notifications(db_user, upliner)
            else:
                logger.debug(f"User {db_user.email} already has correct upliner {upliner.telegramID}")

            await self._update_sheet(user.row_index, 'UplinerFound', '1')
            return True

        except Exception as e:
            logger.error(f"Error assigning upliner for {user.email} row {user.row_index}: {e}")
            return False

    async def _create_purchase(self, session: Session, user: LegacyUserRecord) -> bool:
        """
        Create a purchase for legacy user.
        Protection from duplicates is based on PurchaseDone flag in Google Sheets.
        """
        try:
            # Get user from DB using proper method
            db_user = self._get_user_from_legacy_record(session, user)
            if not db_user:
                logger.debug(f"User {user.email} not found yet, will try again later")
                return False

            # Find project
            project = session.query(Project).filter_by(projectName=user.project).first()
            if not project:
                logger.error(f"Project {user.project} not found for legacy user {user.email}")
                return False

            # Find first option for this project
            option = session.query(Option).filter_by(projectID=project.projectID).first()
            if not option:
                logger.error(f"No options found for project {user.project}")
                return False

            # NO checking for existing purchase by row - trust PurchaseDone flag
            # Check only if this is an additional purchase (for notes)
            has_other_legacy = session.query(ActiveBalance).filter(
                ActiveBalance.userID == db_user.userID,
                ActiveBalance.reason.like('legacy_migration=%')
            ).first() is not None

            # Create purchase with correct price
            total_price = option.costPerShare * user.qty

            purchase = Purchase(
                userID=db_user.userID,
                projectID=project.projectID,
                projectName=project.projectName,
                optionID=option.optionID,
                packQty=user.qty,
                packPrice=total_price,
                createdAt=datetime.utcnow()
            )
            session.add(purchase)
            session.flush()

            # Prepare notes
            notes_text = f'Legacy shares migration'
            if has_other_legacy:
                notes_text += f' (additional purchase)'
            notes_text += f': {user.qty} shares of {project.projectName} at {option.costPerShare} per share'

            # Add balance record WITHOUT row_index
            balance_record = ActiveBalance(
                userID=db_user.userID,
                firstname=db_user.firstname,
                surname=db_user.surname,
                amount=total_price,
                status='done',
                reason=f'legacy_migration={purchase.purchaseID}',  # Simple format without row_index
                notes=notes_text
            )
            session.add(balance_record)
            session.commit()

            await self._update_sheet(user.row_index, 'PurchaseDone', '1')
            await self._send_purchase_notification(db_user, purchase, user)

            logger.info(f"Created legacy purchase {purchase.purchaseID} for user {db_user.email} "
                        f"(${total_price})")
            return True

        except Exception as e:
            logger.error(f"Error creating legacy purchase for {user.email}: {e}")
            return False

    async def _update_sheet(self, row_index: int, field_name: str, value: str):
        field_columns = {'IsFound': 'F', 'UplinerFound': 'G', 'PurchaseDone': 'H'}

        if field_name not in field_columns:
            return

        for attempt in range(3):
            try:
                sheets_client, _ = get_google_services()
                sheet = sheets_client.open_by_key(LEGACY_SHEET_ID).worksheet("Users")
                cell_address = f"{field_columns[field_name]}{row_index}"
                sheet.update(cell_address, value)
                logger.debug(f"Updated sheet {cell_address} = {value}")
                return
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Failed to update sheet {field_name}: {e}")
                else:
                    await asyncio.sleep(2 ** attempt)

    async def _send_welcome_notification(self, user: User, legacy_user: LegacyUserRecord):
        try:
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_user_welcome',
                {'firstname': user.firstname, 'project_name': legacy_user.project, 'qty': legacy_user.qty},
                lang=user.lang
            )

            notification = Notification(
                source="legacy_migration", text=text, buttons=buttons,
                target_type="user", target_value=str(user.userID),
                priority=2, category="legacy", importance="high", parse_mode="HTML"
            )

            with Session() as session:
                session.add(notification)
                session.commit()
        except Exception as e:
            logger.error(f"Error sending legacy welcome notification: {e}")

    async def _send_upliner_notifications(self, user: User, upliner: User):
        try:
            # User notification
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_upliner_assigned_user',
                {'firstname': user.firstname, 'upliner_name': upliner.firstname},
                lang=user.lang
            )
            user_notification = Notification(
                source="legacy_migration", text=text, buttons=buttons,
                target_type="user", target_value=str(user.userID),
                priority=2, category="legacy", importance="normal", parse_mode="HTML"
            )

            # Upliner notification
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_upliner_assigned_upliner',
                {'firstname': upliner.firstname, 'user_name': user.firstname},
                lang=upliner.lang
            )
            upliner_notification = Notification(
                source="legacy_migration", text=text, buttons=buttons,
                target_type="user", target_value=str(upliner.userID),
                priority=2, category="legacy", importance="normal", parse_mode="HTML"
            )

            with Session() as session:
                session.add(user_notification)
                session.add(upliner_notification)
                session.commit()
        except Exception as e:
            logger.error(f"Error sending upliner assigned notifications: {e}")

    async def _send_purchase_notification(self, user: User, purchase: Purchase, legacy_user: LegacyUserRecord):
        try:
            text, buttons = await MessageTemplates.get_raw_template(
                'legacy_purchase_created_user',
                {
                    'firstname': user.firstname, 'qty': legacy_user.qty,
                    'project_name': legacy_user.project, 'purchase_id': purchase.purchaseID
                },
                lang=user.lang
            )

            user_notification = Notification(
                source="legacy_migration", text=text, buttons=buttons,
                target_type="user", target_value=str(user.userID),
                priority=2, category="legacy", importance="high", parse_mode="HTML"
            )

            with Session() as session:
                session.add(user_notification)
                session.commit()
        except Exception as e:
            logger.error(f"Error sending legacy purchase notifications: {e}")


legacy_processor = LegacyUserProcessor()