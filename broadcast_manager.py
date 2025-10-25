"""
Broadcast Manager - Mass notification and email sending system
Sends notifications to Telegram bot and emails based on Google Sheets data
"""

import logging
import asyncio
from typing import Dict, List, Optional
from datetime import datetime
from database import User, Notification
from init import get_session
from templates import MessageTemplates, SafeDict
from google_services import get_google_services
from email_sender import email_manager
import config

logger = logging.getLogger(__name__)

# Get session factory
logger.info(f"broadcast_manager: Creating session with DATABASE_URL: {config.DATABASE_URL}")
SessionFactory, _ = get_session()
logger.info("broadcast_manager: Session factory created")

# Configuration
BROADCAST_SHEET_URL = "https://docs.google.com/spreadsheets/d/1SeymB8GE2Zl6XQ4g3xIpDBHUkNFdp9yDMGUoYrBMKNg"
BROADCAST_SHEET_NAME = "Recipients"  # Name of the sheet/tab with recipient data
BATCH_SIZE = 50  # Emails per batch
BATCH_DELAY = 3  # Seconds between batches
PROGRESS_REPORT_EVERY = 200  # Send progress update every N recipients


class BroadcastManager:
    """Manages mass broadcast notifications and emails"""

    def __init__(self):
        self.google_services = None
        self.stats = {
            'total_recipients': 0,
            'bot_sent': 0,
            'bot_failed': 0,
            'email_sent': 0,
            'email_failed': 0,
            'email_skipped': 0,  # Added for --nomail tracking
            'not_found_in_db': 0,
            'errors': []
        }
        self.is_running = False
        self.should_cancel = False
        self.current_task = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize Google Services"""
        try:
            sheets_client, drive_service = get_google_services()
            # Store sheets client for API calls
            self.google_services = sheets_client
            logger.info("BroadcastManager initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize BroadcastManager: {e}")
            raise

    def cancel_broadcast(self):
        """Cancel currently running broadcast"""
        if self.is_running:
            self.should_cancel = True
            logger.info("Broadcast cancellation requested")
            return True
        return False

    def get_status(self) -> Dict:
        """Get current broadcast status"""
        return {
            'is_running': self.is_running,
            'stats': self.stats.copy()
        }

    def _extract_sheet_id(self, url: str) -> str:
        """Extract sheet ID from Google Sheets URL"""
        # Extract ID from URL like: https://docs.google.com/spreadsheets/d/{SHEET_ID}/...
        if '/d/' in url:
            return url.split('/d/')[1].split('/')[0]
        return url

    async def read_recipients_from_sheet(self, sheet_url: str, test_mode: bool = False) -> List[Dict]:
        """
        Read recipients data from Google Sheets

        Args:
            sheet_url: URL of Google Sheets document
            test_mode: If True, only read first 10 rows

        Returns:
            List of recipient dictionaries
        """
        try:
            sheet_id = self._extract_sheet_id(sheet_url)
            logger.info(f"Reading recipients from sheet: {sheet_id}, tab: {BROADCAST_SHEET_NAME}")

            # Open spreadsheet and get worksheet using gspread
            spreadsheet = self.google_services.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet(BROADCAST_SHEET_NAME)

            # Get all values
            all_values = worksheet.get_all_values()

            if not all_values:
                logger.warning("No data found in sheet")
                return []

            # In test mode, limit to first 11 rows (header + 10 data rows)
            if test_mode:
                all_values = all_values[:11]

            # Parse headers (first row)
            headers = all_values[0]
            logger.info(f"Found headers: {headers}")

            # Parse data rows
            recipients = []
            for idx, row in enumerate(all_values[1:], start=2):  # Skip header row
                try:
                    # Ensure row has enough columns
                    while len(row) < len(headers):
                        row.append('')

                    recipient = {}
                    for i, header in enumerate(headers):
                        recipient[header.strip()] = row[i].strip() if i < len(row) else ''

                    # Validate that we have at least UserID or TelegramID or Email
                    has_user_id = recipient.get('UserID', '').strip()
                    has_telegram_id = recipient.get('TelegramID', '').strip()
                    has_email = recipient.get('Email', '').strip()

                    if not (has_user_id or has_telegram_id or has_email):
                        logger.warning(f"Row {idx}: No UserID, TelegramID or Email - skipping")
                        continue

                    recipients.append(recipient)

                except Exception as e:
                    logger.error(f"Error parsing row {idx}: {e}")
                    continue

            logger.info(f"Successfully parsed {len(recipients)} recipients from sheet")
            return recipients

        except Exception as e:
            logger.error(f"Error reading sheet: {e}")
            raise

    def find_user_in_db(self, user_id: Optional[str], telegram_id: Optional[str]) -> Optional[tuple]:
        """
        Find user in database by UserID or TelegramID

        Args:
            user_id: User ID from sheet (as string)
            telegram_id: Telegram ID from sheet (as string)

        Returns:
            Tuple of (field_name, value) or None
        """
        try:
            logger.info(f"find_user_in_db: Input - user_id='{user_id}' (type: {type(user_id)}), telegram_id='{telegram_id}' (type: {type(telegram_id)})")

            # Try to find by TelegramID FIRST (more reliable)
            if telegram_id and telegram_id.strip().isdigit():
                telegram_id_int = int(telegram_id.strip())
                logger.info(f"find_user_in_db: Returning search by telegramID={telegram_id_int}")
                return ('telegramID', telegram_id_int)

            # Fallback to UserID
            if user_id and user_id.strip().isdigit():
                user_id_int = int(user_id.strip())
                logger.info(f"find_user_in_db: Returning search by userID={user_id_int}")
                return ('userID', user_id_int)

            logger.warning(f"find_user_in_db: No valid ID found - user_id='{user_id}', telegram_id='{telegram_id}'")
            return None

        except Exception as e:
            logger.error(f"Error finding user (UserID: {user_id}, TelegramID: {telegram_id}): {e}", exc_info=True)
            return None

    def _get_user_from_db(self, session, search_tuple: tuple) -> Optional[User]:
        """
        Internal method to get user from database with active session

        Args:
            session: Active SQLAlchemy session
            search_tuple: Tuple of (field_name, value)

        Returns:
            User object or None
        """
        try:
            if not search_tuple:
                logger.warning("_get_user_from_db: search_tuple is None")
                return None

            field_name, value = search_tuple
            logger.info(f"_get_user_from_db: Searching by {field_name}={value} (type: {type(value)})")

            if field_name == 'userID':
                user = session.query(User).filter_by(userID=value).first()
                logger.info(f"_get_user_from_db: Query by userID={value} returned: {user}")
                return user
            elif field_name == 'telegramID':
                user = session.query(User).filter_by(telegramID=value).first()
                logger.info(f"_get_user_from_db: Query by telegramID={value} returned: {user}")
                return user

            logger.warning(f"_get_user_from_db: Unknown field_name: {field_name}")
            return None

        except Exception as e:
            logger.error(f"Error querying user by {field_name}={value}: {e}", exc_info=True)
            return None

    def _fix_telegram_html(self, text: str) -> str:
        """
        Replace <br> tags with newlines for Telegram HTML parser
        Telegram doesn't support <br> tags, only actual newlines

        Args:
            text: HTML text with <br> tags

        Returns:
            Text with <br> replaced by \n
        """
        return text.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')

    async def create_bot_notification(self, user: User, template: Dict, variables: Dict, session=None) -> bool:
        """
        Create notification in database for bot delivery

        Args:
            user: User object from database
            template: Template dict (from get_template)
            variables: Variables for template formatting
            session: Optional existing session (if None, creates own)

        Returns:
            True if notification created successfully
        """
        try:
            # Use provided session or create new one
            should_close = False
            if session is None:
                session = SessionFactory()
                session.__enter__()
                should_close = True

            try:
                # Extract raw text and buttons from template dict
                text_template = template['text']
                buttons = template['buttons']

                # Format text with variables using SafeDict for safety
                formatted_text = text_template.format_map(SafeDict(variables))
                formatted_text = self._fix_telegram_html(formatted_text)  # Fix <br> tags

                # Format buttons if they exist
                formatted_buttons = None
                if buttons:
                    formatted_buttons = buttons.format_map(SafeDict(variables))

                # Create notification
                notification = Notification(
                    source="broadcast",
                    text=formatted_text,
                    buttons=formatted_buttons,
                    target_type="user",
                    target_value=str(user.userID),
                    priority=2,
                    category="broadcast",
                    importance="high",
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )

                session.add(notification)

                # Only commit if we created our own session
                if should_close:
                    session.commit()

                logger.info(f"Created bot notification for user {user.userID}")
                return True

            finally:
                # Only close if we created the session
                if should_close:
                    session.__exit__(None, None, None)

        except Exception as e:
            logger.error(f"Error creating bot notification for user {user.userID if user else 'unknown'}: {e}")
            return False

    async def send_email_notification(
            self,
            email: str,
            subject_template: Dict,
            body_template: Dict,
            variables: Dict
    ) -> bool:
        """
        Send email notification with proper variable substitution

        Args:
            email: Recipient email address
            subject_template: Template dict for subject (from get_template)
            body_template: Template dict for body (from get_template)
            variables: Variables for template formatting

        Returns:
            True if email sent successfully
        """
        try:
            # Extract text from template dicts
            subject_text = subject_template['text']
            body_text = body_template['text']

            # Format templates with variables using SafeDict
            subject = subject_text.format_map(SafeDict(variables))
            body = body_text.format_map(SafeDict(variables))

            # Send email
            success = await email_manager.send_notification_email(
                to=email,
                subject=subject,
                body=body
            )

            if success:
                logger.info(f"Email sent to {email}")
            else:
                logger.warning(f"Failed to send email to {email}")

            return success

        except Exception as e:
            logger.error(f"Error sending email to {email}: {e}")
            return False

    async def process_single_recipient(
            self,
            row_number: int,
            recipient_data: Dict,
            skip_email: bool = False
    ) -> Dict:
        """
        Process single recipient - send to bot and/or email
        Templates are loaded on-demand based on user's language

        Args:
            row_number: Row number in sheet (for error reporting)
            recipient_data: Dictionary with recipient data
            skip_email: If True, skip email sending

        Returns:
            Dictionary with processing results
        """
        result = {
            'bot_sent': False,
            'email_sent': False,
            'user_found': False,
            'errors': []
        }

        user_id = recipient_data.get('UserID', '').strip()
        telegram_id = recipient_data.get('TelegramID', '').strip()
        email = recipient_data.get('Email', '').strip()
        firstname = recipient_data.get('Firstname', '').strip()
        lastname = recipient_data.get('Lastname', '').strip()
        amount = recipient_data.get('Amount of DARWIN in USD', '').strip()
        broker_code = recipient_data.get('#DW', '').strip()

        # Prepare variables for templates
        variables = {
            'firstname': firstname or 'User',
            'lastname': lastname or '',
            'email': email or 'N/A',
            'amount': amount or '0',
            'user_id': user_id or 'N/A',
            'telegram_id': telegram_id or 'N/A',
            'broker_code': broker_code or 'N/A'
        }

        # Try to find user in database
        search_tuple = self.find_user_in_db(user_id, telegram_id)
        user = None
        user_lang = 'en'  # Default language

        if search_tuple:
            # Get user with active session
            with SessionFactory() as session:
                user = self._get_user_from_db(session, search_tuple)

                if user:
                    result['user_found'] = True
                    # Use user's language from DB (auto-fallback to 'en' in get_template)
                    user_lang = user.lang or 'en'
                    logger.info(f"User {user.userID} language: {user_lang}")

                    # Send bot notification
                    try:
                        # Get template for user's language (auto-fallback to English if not exists)
                        bot_template = await MessageTemplates.get_template('broadcast_bot', lang=user_lang)

                        if not bot_template:
                            logger.error(f"Bot template not found for any language")
                            result['errors'].append("Bot template not found")
                        else:
                            bot_success = await self.create_bot_notification(
                                user,
                                bot_template,
                                variables,
                                session=session
                            )
                            result['bot_sent'] = bot_success

                            if bot_success:
                                # Mark user as received DARWIN broadcast in notes
                                import helpers
                                helpers.set_user_note(user, 'dwBroadcast', '1')
                                if broker_code:
                                    helpers.set_user_note(user, 'dwBrokerCode', broker_code)
                                session.commit()
                                logger.info(f"Marked user {user.userID} as received DARWIN broadcast with code {broker_code}")
                            else:
                                result['errors'].append(f"Failed to create bot notification")

                    except Exception as e:
                        logger.error(f"Error processing bot notification for row {row_number}: {e}")
                        result['errors'].append(f"Bot notification error: {str(e)}")

        if not user:
            # User not found in DB
            result['errors'].append("User not found in database")
            logger.warning(f"Row {row_number}: User not found - UserID: {user_id}, TelegramID: {telegram_id}")

        # Send email if email address provided and not skipping emails
        if email and not skip_email:
            try:
                # Get email templates for user's language (auto-fallback to English)
                email_subject_template = await MessageTemplates.get_template('broadcast_email_subject', lang=user_lang)
                email_body_template = await MessageTemplates.get_template('broadcast_email_body', lang=user_lang)

                if not email_subject_template or not email_body_template:
                    logger.error(f"Email templates not found for language {user_lang}")
                    result['errors'].append("Email templates not found")
                else:
                    email_success = await self.send_email_notification(
                        email,
                        email_subject_template,
                        email_body_template,
                        variables
                    )
                    result['email_sent'] = email_success

                    if not email_success:
                        result['errors'].append(f"Failed to send email")

            except Exception as e:
                logger.error(f"Error processing email for row {row_number}: {e}")
                result['errors'].append(f"Email error: {str(e)}")
        elif email and skip_email:
            logger.debug(f"Skipping email for row {row_number} due to --nomail flag")

        return result

    async def run_broadcast(
            self,
            sheet_url: str = BROADCAST_SHEET_URL,
            test_mode: bool = False,
            skip_email: bool = False,
            progress_callback=None
    ) -> Dict:
        """
        Main method to run broadcast campaign

        Args:
            sheet_url: URL of Google Sheets with recipients
            test_mode: If True, only process first 10 recipients
            skip_email: If True, skip email sending (--nomail flag)
            progress_callback: Optional async callback for progress updates

        Returns:
            Dictionary with campaign statistics
        """
        # Use lock to prevent race condition
        async with self._lock:
            # Check if already running
            if self.is_running:
                logger.warning("Broadcast is already running")
                return {'error': 'Broadcast is already running'}

            self.is_running = True
            self.should_cancel = False

        start_time = datetime.now()
        logger.info(f"Starting broadcast {'(TEST MODE)' if test_mode else ''} {'(NO EMAIL)' if skip_email else ''}")

        try:
            # Initialize if needed
            if not self.google_services:
                await self.initialize()
                if not self.google_services:
                    raise Exception("Failed to initialize Google Services")

            # Reset statistics
            self.stats = {
                'total_recipients': 0,
                'bot_sent': 0,
                'bot_failed': 0,
                'email_sent': 0,
                'email_failed': 0,
                'email_skipped': 0,
                'not_found_in_db': 0,
                'errors': []
            }

            # Load recipients
            recipients = await self.read_recipients_from_sheet(sheet_url, test_mode)
            if not recipients:
                logger.error("No recipients found in sheet")
                return self.stats

            self.stats['total_recipients'] = len(recipients)
            logger.info(f"Processing {len(recipients)} recipients")
            logger.info("Templates will be loaded per-user based on their language with auto-fallback to English")

            # Process recipients in batches
            email_batch = []
            processed_count = 0

            for idx, recipient_data in enumerate(recipients, start=1):
                # Check for cancellation
                if self.should_cancel:
                    logger.info(f"Broadcast cancelled by user at {processed_count}/{len(recipients)}")
                    self.stats['cancelled'] = True
                    self.stats['cancelled_at'] = processed_count
                    break

                try:
                    # Process recipient with skip_email flag
                    result = await self.process_single_recipient(
                        row_number=idx + 1,  # +1 because of header row
                        recipient_data=recipient_data,
                        skip_email=skip_email
                    )

                    # Update statistics
                    if result['bot_sent']:
                        self.stats['bot_sent'] += 1
                    elif result['user_found']:
                        self.stats['bot_failed'] += 1

                    if skip_email and recipient_data.get('Email', '').strip():
                        self.stats['email_skipped'] += 1
                    elif result['email_sent']:
                        self.stats['email_sent'] += 1
                        email_batch.append(recipient_data.get('Email', ''))
                    elif recipient_data.get('Email', '').strip():
                        self.stats['email_failed'] += 1

                    if not result['user_found'] and not recipient_data.get('Email', '').strip():
                        self.stats['not_found_in_db'] += 1

                    # Record errors
                    if result['errors']:
                        self.stats['errors'].append({
                            'row': idx + 1,
                            'user_id': recipient_data.get('UserID', ''),
                            'telegram_id': recipient_data.get('TelegramID', ''),
                            'email': recipient_data.get('Email', ''),
                            'errors': result['errors']
                        })

                    processed_count += 1

                    # Send progress report
                    if progress_callback and processed_count % PROGRESS_REPORT_EVERY == 0:
                        await progress_callback(processed_count, self.stats.copy())

                    # Batch delay for emails (only if not skipping emails)
                    if not skip_email and len(email_batch) >= BATCH_SIZE:
                        logger.info(f"Processed batch of {len(email_batch)} emails, waiting {BATCH_DELAY}s...")
                        await asyncio.sleep(BATCH_DELAY)
                        email_batch = []

                except Exception as e:
                    logger.error(f"Error processing recipient {idx}: {e}")
                    self.stats['errors'].append({
                        'row': idx + 1,
                        'user_id': recipient_data.get('UserID', ''),
                        'telegram_id': recipient_data.get('TelegramID', ''),
                        'email': recipient_data.get('Email', ''),
                        'errors': [str(e)]
                    })

            # Final batch delay if any emails left
            if not skip_email and email_batch:
                await asyncio.sleep(BATCH_DELAY)

            # Calculate duration
            duration = datetime.now() - start_time
            self.stats['duration'] = str(duration)
            self.stats['test_mode'] = test_mode
            self.stats['skip_email'] = skip_email

            logger.info(f"Broadcast completed in {duration}")
            return self.stats

        except Exception as e:
            logger.error(f"Critical error in broadcast: {e}")
            self.stats['critical_error'] = str(e)
            return self.stats
        finally:
            self.is_running = False
            self.should_cancel = False

    def format_report(self, stats: Dict) -> str:
        """
        Format broadcast statistics into readable report

        Args:
            stats: Statistics dictionary

        Returns:
            Formatted report string
        """
        test_marker = "🧪 TEST MODE\n\n" if stats.get('test_mode', False) else ""
        cancelled_marker = "🛑 CANCELLED\n\n" if stats.get('cancelled', False) else ""
        no_email_marker = "📧 EMAIL SKIPPED\n\n" if stats.get('skip_email', False) else ""

        report = f"{test_marker}{cancelled_marker}{no_email_marker}📊 <b>Broadcast Report</b>\n\n"
        report += f"👥 Total recipients: {stats['total_recipients']}\n"

        if stats.get('cancelled'):
            report += f"⏹ Processed before cancel: {stats.get('cancelled_at', 0)}\n\n"

        report += f"✅ Bot sent: {stats['bot_sent']}\n"
        report += f"❌ Bot failed: {stats['bot_failed']}\n"

        if not stats.get('skip_email'):
            report += f"📧 Email sent: {stats['email_sent']}\n"
            report += f"❌ Email failed: {stats['email_failed']}\n"
        else:
            report += f"⏭ Email skipped: {stats.get('email_skipped', 0)}\n"

        report += f"⚠️ Not found in DB: {stats['not_found_in_db']}\n"

        if stats.get('duration'):
            report += f"⏱ Duration: {stats['duration']}\n"

        if stats.get('critical_error'):
            report += f"\n🔴 <b>Critical Error:</b>\n{stats['critical_error']}\n"

        # Add errors summary
        if stats['errors']:
            report += f"\n\n❌ <b>Errors ({len(stats['errors'])}):</b>\n"
            for i, error in enumerate(stats['errors'][:10], 1):
                report += f"\n{i}. Row {error['row']}"
                if error.get('user_id'):
                    report += f" | UserID: {error['user_id']}"
                if error.get('telegram_id'):
                    report += f" | TgID: {error['telegram_id']}"
                if error.get('email'):
                    report += f" | Email: {error['email'][:20]}..."
                report += f"\n   {', '.join(error['errors'])}\n"

            if len(stats['errors']) > 10:
                report += f"\n... and {len(stats['errors']) - 10} more errors\n"

        return report


# Create singleton instance
broadcast_manager = BroadcastManager()