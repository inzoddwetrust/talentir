"""
Broadcast Manager - Mass notification and email sending system
Sends notifications to Telegram bot and emails based on Google Sheets data
"""

import logging
import asyncio
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from database import User, Notification
from init import get_session
from templates import MessageTemplates
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
            'not_found_in_db': 0,
            'errors': []
        }
        self.is_running = False
        self.should_cancel = False
        self.current_task = None

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

    async def load_templates(self) -> Dict[str, Tuple[str, str]]:
        """
        Load broadcast templates from Google Sheets

        Returns:
            Dictionary with templates for both languages
        """
        try:
            templates = {}

            # Load bot template (returns tuple: (text, buttons))
            bot_en = await MessageTemplates.get_raw_template('broadcast_bot', {}, lang='en')
            bot_ru = await MessageTemplates.get_raw_template('broadcast_bot', {}, lang='ru')

            # Load email templates (subject and body)
            email_subject_en = await MessageTemplates.get_raw_template('broadcast_email_subject', {}, lang='en')
            email_subject_ru = await MessageTemplates.get_raw_template('broadcast_email_subject', {}, lang='ru')

            email_body_en = await MessageTemplates.get_raw_template('broadcast_email_body', {}, lang='en')
            email_body_ru = await MessageTemplates.get_raw_template('broadcast_email_body', {}, lang='ru')

            templates = {
                'bot_en': bot_en,
                'bot_ru': bot_ru,
                'email_subject_en': email_subject_en,
                'email_subject_ru': email_subject_ru,
                'email_body_en': email_body_en,
                'email_body_ru': email_body_ru
            }

            logger.info("Templates loaded successfully")
            return templates

        except Exception as e:
            logger.error(f"Error loading templates: {e}")
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

            # Debug: Check total users in DB
            total_users = session.query(User).count()
            logger.info(f"_get_user_from_db: Total users in database: {total_users}")

            # Debug: Show ALL users in DB (if small number)
            if total_users <= 5:
                all_users = session.query(User.userID, User.telegramID, User.firstname).all()
                logger.info(f"_get_user_from_db: ALL USERS IN DB: {[(u.userID, u.telegramID, u.firstname) for u in all_users]}")

            if field_name == 'userID':
                user = session.query(User).filter_by(userID=value).first()
                logger.info(f"_get_user_from_db: Query by userID={value} returned: {user}")
                return user
            elif field_name == 'telegramID':
                user = session.query(User).filter_by(telegramID=value).first()
                logger.info(f"_get_user_from_db: Query by telegramID={value} returned: {user}")

                # Debug: Try to find any user with similar telegramID
                if not user:
                    similar = session.query(User.telegramID).filter(
                        User.telegramID.like(f'%{str(value)[-4:]}')
                    ).limit(5).all()
                    logger.info(f"_get_user_from_db: Similar telegramIDs in DB: {[s.telegramID for s in similar]}")

                return user

            logger.warning(f"_get_user_from_db: Unknown field_name: {field_name}")
            return None

        except Exception as e:
            logger.error(f"Error querying user by {field_name}={value}: {e}", exc_info=True)
            return None

    def format_template(self, template_text: str, variables: Dict) -> str:
        """
        Format template with variables

        Args:
            template_text: Template string with {placeholders}
            variables: Dictionary with variable values

        Returns:
            Formatted string
        """
        try:
            return template_text.format(**variables)
        except KeyError as e:
            logger.warning(f"Missing variable in template: {e}")
            return template_text
        except Exception as e:
            logger.error(f"Error formatting template: {e}")
            return template_text

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

    async def create_bot_notification(self, user: User, template: Tuple[str, str], variables: Dict) -> bool:
        """
        Create notification in database for bot delivery

        Args:
            user: User object from database
            template: Tuple of (text, buttons) from template
            variables: Variables for template formatting

        Returns:
            True if notification created successfully
        """
        try:
            with SessionFactory() as session:
                text_template, buttons = template

                # Format text with variables and fix Telegram HTML
                formatted_text = self.format_template(text_template, variables)
                formatted_text = self._fix_telegram_html(formatted_text)  # Fix <br> tags

                # Create notification
                notification = Notification(
                    source="broadcast",
                    text=formatted_text,
                    buttons=buttons,  # Buttons already in correct format from template
                    target_type="user",
                    target_value=str(user.userID),
                    priority=2,  # High priority
                    category="broadcast",
                    importance="high",
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )

                session.add(notification)
                session.commit()

                logger.info(f"Created bot notification for user {user.userID}")
                return True

        except Exception as e:
            logger.error(f"Error creating bot notification for user {user.userID if user else 'unknown'}: {e}")
            return False

    async def send_email_notification(
        self,
        email: str,
        subject_template: Tuple[str, str],
        body_template: Tuple[str, str],
        variables: Dict
    ) -> bool:
        """
        Send email notification

        Args:
            email: Recipient email address
            subject_template: Tuple of (text, buttons) for subject
            body_template: Tuple of (text, buttons) for body
            variables: Variables for template formatting

        Returns:
            True if email sent successfully
        """
        try:
            # Extract text from tuples (ignore buttons for email)
            subject_text = subject_template[0] if isinstance(subject_template, tuple) else subject_template
            body_text = body_template[0] if isinstance(body_template, tuple) else body_template

            # Format templates
            subject = self.format_template(subject_text, variables)
            body = self.format_template(body_text, variables)

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
        templates: Dict
    ) -> Dict:
        """
        Process single recipient - send to bot and/or email

        Args:
            row_number: Row number in sheet (for error reporting)
            recipient_data: Dictionary with recipient data
            templates: Dictionary with all templates

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
        broker_code = recipient_data.get('#DW', '').strip()  # Extract broker code from column H

        # Prepare variables for templates
        variables = {
            'firstname': firstname or 'User',
            'lastname': lastname or '',
            'email': email or 'N/A',
            'amount': amount or '0',
            'user_id': user_id or 'N/A',
            'telegram_id': telegram_id or 'N/A',
            'broker_code': broker_code or 'N/A'  # Add broker code to variables
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
                    user_lang = user.lang if user.lang in ['en', 'ru'] else 'en'

                    # Send bot notification
                    try:
                        bot_template = templates[f'bot_{user_lang}']
                        bot_success = await self.create_bot_notification(user, bot_template, variables)
                        result['bot_sent'] = bot_success

                        if bot_success:
                            # Mark user as received DARWIN broadcast in notes
                            import helpers
                            helpers.set_user_note(user, 'dwBroadcast', '1')
                            # Save broker code to notes for future reference
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

        # Send email if email address provided
        if email:
            try:
                subject_template = templates[f'email_subject_{user_lang}']
                body_template = templates[f'email_body_{user_lang}']

                email_success = await self.send_email_notification(
                    email,
                    subject_template,
                    body_template,
                    variables
                )
                result['email_sent'] = email_success

                if not email_success:
                    result['errors'].append(f"Failed to send email")

            except Exception as e:
                logger.error(f"Error processing email for row {row_number}: {e}")
                result['errors'].append(f"Email error: {str(e)}")

        return result

    async def run_broadcast(
        self,
        sheet_url: str = BROADCAST_SHEET_URL,
        test_mode: bool = False,
        progress_callback=None
    ) -> Dict:
        """
        Main method to run broadcast campaign

        Args:
            sheet_url: URL of Google Sheets with recipients
            test_mode: If True, only process first 10 recipients
            progress_callback: Optional async callback for progress updates

        Returns:
            Dictionary with campaign statistics
        """
        # Check if already running
        if self.is_running:
            logger.warning("Broadcast is already running")
            return {'error': 'Broadcast is already running'}

        self.is_running = True
        self.should_cancel = False
        start_time = datetime.now()
        logger.info(f"Starting broadcast {'(TEST MODE)' if test_mode else ''}")

        try:
            # Initialize if needed
            if not self.google_services:
                await self.initialize()

            # Load recipients
            recipients = await self.read_recipients_from_sheet(sheet_url, test_mode)
            if not recipients:
                logger.error("No recipients found in sheet")
                return self.stats

            self.stats['total_recipients'] = len(recipients)
            logger.info(f"Processing {len(recipients)} recipients")

            # Load templates
            templates = await self.load_templates()

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
                    # Process recipient
                    result = await self.process_single_recipient(
                        row_number=idx + 1,  # +1 because of header row
                        recipient_data=recipient_data,
                        templates=templates
                    )

                    # Update statistics
                    if result['bot_sent']:
                        self.stats['bot_sent'] += 1
                    elif result['user_found']:
                        self.stats['bot_failed'] += 1

                    if result['email_sent']:
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

                    # Batch delay for emails
                    if len(email_batch) >= BATCH_SIZE:
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
            if email_batch:
                await asyncio.sleep(BATCH_DELAY)

            # Calculate duration
            duration = datetime.now() - start_time
            self.stats['duration'] = str(duration)
            self.stats['test_mode'] = test_mode

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

        report = f"{test_marker}{cancelled_marker}📊 <b>Broadcast Report</b>\n\n"
        report += f"👥 Total recipients: {stats['total_recipients']}\n"

        if stats.get('cancelled'):
            report += f"⏹ Processed before cancel: {stats.get('cancelled_at', 0)}\n\n"

        report += f"✅ Bot sent: {stats['bot_sent']}\n"
        report += f"❌ Bot failed: {stats['bot_failed']}\n"
        report += f"📧 Email sent: {stats['email_sent']}\n"
        report += f"❌ Email failed: {stats['email_failed']}\n"
        report += f"⚠️ Not found in DB: {stats['not_found_in_db']}\n"

        if stats.get('duration'):
            report += f"⏱ Duration: {stats['duration']}\n"

        if stats.get('critical_error'):
            report += f"\n🔴 <b>Critical Error:</b>\n{stats['critical_error']}\n"

        # Add errors summary
        if stats['errors']:
            report += f"\n\n❌ <b>Errors ({len(stats['errors'])}):</b>\n"
            for i, error in enumerate(stats['errors'][:10], 1):  # Show first 10 errors
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