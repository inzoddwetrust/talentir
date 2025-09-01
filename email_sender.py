import logging
import aiosmtplib
from typing import Dict, Any, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import config
from templates import MessageTemplates
import aiohttp
import base64
from typing import List

logger = logging.getLogger(__name__)


class SMTPProvider:
    """SMTP provider for own mail server"""

    def __init__(self, smtp_host: str, smtp_port: int, username: str, password: str):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password

    async def send_email(self, to: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
        """
        Send email via SMTP server

        Args:
            to: Recipient email
            subject: Email subject
            html_body: HTML version of email
            text_body: Text version of email (optional)

        Returns:
            bool: True if email sent successfully
        """
        try:
            logger.info(f"Attempting to send email via SMTP to {to} with subject: {subject}")

            # Create message
            if text_body:
                # If both HTML and text - create multipart
                message = MIMEMultipart('alternative')
                message['From'] = f"JetUp <{config.SMTP_USER}>"
                message['To'] = to
                message['Subject'] = subject

                # Add text and HTML parts
                text_part = MIMEText(text_body, 'plain', 'utf-8')
                html_part = MIMEText(html_body, 'html', 'utf-8')

                message.attach(text_part)
                message.attach(html_part)
            else:
                # HTML only version
                message = MIMEText(html_body, 'html', 'utf-8')
                message['From'] = f"JetUp <{config.SMTP_USER}>"
                message['To'] = to
                message['Subject'] = subject

            # Send via SMTP
            await aiosmtplib.send(
                message,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.username,
                password=self.password,
                start_tls=True,
                use_tls=False,
                timeout=30
            )

            logger.info(f"Email sent successfully via SMTP to {to}")
            return True

        except aiosmtplib.SMTPException as e:
            logger.error(f"SMTP error while sending email to {to}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error while sending email via SMTP to {to}: {e}")
            logger.exception("Full traceback:")
            return False

    async def test_connection(self) -> bool:
        """
        Test connection to SMTP server

        Returns:
            bool: True if connection successful
        """
        try:
            logger.info(f"Testing SMTP connection to {self.smtp_host}:{self.smtp_port}")

            # Try to connect and authenticate
            smtp = aiosmtplib.SMTP(hostname=self.smtp_host, port=self.smtp_port, use_tls=False)
            await smtp.connect()

            # Only start TLS if not already secured
            if not smtp.is_connected:
                return False

            if self.smtp_port == 587:  # STARTTLS port
                await smtp.starttls()

            await smtp.login(self.username, self.password)
            await smtp.quit()

            logger.info("SMTP connection test successful")
            return True

        except Exception as e:
            logger.error(f"SMTP connection test failed: {e}")
            return False


class MailgunProvider:
    """Mailgun provider for secure email domains"""

    def __init__(self, api_key: str, domain: str, region: str = 'eu'):
        self.api_key = api_key
        self.domain = domain
        self.region = region.lower()

        # Set base URL based on region
        if self.region == 'eu':
            self.base_url = "https://api.eu.mailgun.net/v3"
        else:
            self.base_url = "https://api.mailgun.net/v3"

    async def send_email(self, to: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
        """
        Send email via Mailgun API

        Args:
            to: Recipient email
            subject: Email subject
            html_body: HTML version of email
            text_body: Text version of email (optional)

        Returns:
            bool: True if email sent successfully
        """
        try:
            logger.info(f"Attempting to send email via Mailgun to {to} with subject: {subject}")

            # Prepare request data
            data = aiohttp.FormData()
            data.add_field('from', f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>")
            data.add_field('to', to)
            data.add_field('subject', subject)
            data.add_field('html', html_body)

            if text_body:
                data.add_field('text', text_body)

            # Send request with proper auth
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{self.base_url}/{self.domain}/messages",
                        auth=aiohttp.BasicAuth("api", self.api_key),
                        data=data,
                        timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    response_text = await response.text()

                    if response.status == 200:
                        logger.info(f"Email sent successfully via Mailgun to {to}")
                        logger.debug(f"Mailgun response: {response_text}")
                        return True
                    else:
                        logger.error(f"Mailgun error {response.status}: {response_text}")
                        return False

        except aiohttp.ClientError as e:
            logger.error(f"Mailgun network error while sending email to {to}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error while sending email via Mailgun to {to}: {e}")
            logger.exception("Full traceback:")
            return False

    async def test_connection(self) -> bool:
        """
        Test connection to Mailgun API

        Returns:
            bool: True if connection successful
        """
        try:
            logger.info(f"Testing Mailgun connection to {self.base_url}")

            # Try to get domain info
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{self.base_url}/domains/{self.domain}",
                        auth=aiohttp.BasicAuth("api", self.api_key),
                        timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        domain_info = await response.json()
                        logger.info(
                            f"Mailgun connection test successful. Domain state: {domain_info.get('domain', {}).get('state', 'unknown')}")
                        return True
                    else:
                        response_text = await response.text()
                        logger.error(f"Mailgun connection test failed: {response.status} - {response_text}")
                        return False

        except Exception as e:
            logger.error(f"Mailgun connection test failed: {e}")
            return False

class EmailManager:
    """Manager for email operations through multiple providers"""

    def __init__(self):
        self.providers = {}  # Changed from list to dict for named providers
        self.secure_domains = []  # List of domains that should use Mailgun

        # Load secure domains from config
        self._load_secure_domains()

        # Initialize SMTP provider
        if all([
            hasattr(config, 'SMTP_HOST') and config.SMTP_HOST,
            hasattr(config, 'SMTP_USER') and config.SMTP_USER,
            hasattr(config, 'SMTP_PASSWORD') and config.SMTP_PASSWORD
        ]):
            smtp_port = getattr(config, 'SMTP_PORT', 587)
            self.providers['smtp'] = SMTPProvider(
                config.SMTP_HOST,
                smtp_port,
                config.SMTP_USER,
                config.SMTP_PASSWORD
            )
            logger.info(f"EmailManager: Added SMTP provider ({config.SMTP_HOST}:{smtp_port})")

        # Initialize Mailgun provider
        if all([
            hasattr(config, 'MAILGUN_API_KEY') and config.MAILGUN_API_KEY,
            hasattr(config, 'MAILGUN_DOMAIN') and config.MAILGUN_DOMAIN
        ]):
            mailgun_region = getattr(config, 'MAILGUN_REGION', 'eu')
            self.providers['mailgun'] = MailgunProvider(
                config.MAILGUN_API_KEY,
                config.MAILGUN_DOMAIN,
                mailgun_region
            )
            logger.info(
                f"EmailManager: Added Mailgun provider (domain: {config.MAILGUN_DOMAIN}, region: {mailgun_region})")

        if not self.providers:
            logger.warning("EmailManager: No email providers configured")

    def _load_secure_domains(self):
        """Load list of secure email domains from config"""
        try:
            # Try to get from dynamically loaded config attributes
            domains_str = getattr(config, 'SECURE_EMAIL_DOMAINS', '')

            if domains_str:
                # Parse domains, remove spaces, ensure @ prefix
                domains = [d.strip() for d in domains_str.split(',') if d.strip()]
                # Ensure all domains start with @
                self.secure_domains = [d if d.startswith('@') else f'@{d}' for d in domains]
                logger.info(f"Loaded secure domains: {self.secure_domains}")
            else:
                self.secure_domains = []
                logger.info("No secure domains configured")

        except Exception as e:
            logger.warning(f"Could not load secure domains: {e}")
            self.secure_domains = []

    def _get_email_domain(self, email: str) -> str:
        """Extract domain from email address"""
        if '@' in email:
            return '@' + email.split('@')[1].lower()
        return ''

    def _select_provider_for_email(self, email: str) -> List[str]:
        """
        Select provider order based on recipient email domain

        Args:
            email: Recipient email address

        Returns:
            List of provider names in priority order
        """
        domain = self._get_email_domain(email)

        # Check if domain is in secure list
        if domain in self.secure_domains:
            logger.info(f"Domain {domain} is in secure list, prioritizing Mailgun")
            # Secure domain: try Mailgun first, then SMTP as fallback
            provider_order = ['mailgun', 'smtp']
        else:
            logger.info(f"Domain {domain} is not in secure list, prioritizing SMTP")
            # Regular domain: try SMTP first, then Mailgun as fallback
            provider_order = ['smtp', 'mailgun']

        # Filter to only available providers
        available_order = [p for p in provider_order if p in self.providers]
        logger.info(f"Provider order for {email}: {available_order}")

        return available_order

    async def send_verification_email(self, user, verification_link: str) -> bool:
        """
        Send verification email to user through available providers

        Args:
            user: User object from DB
            verification_link: Verification link

        Returns:
            bool: True if email sent successfully
        """
        if not self.providers:
            logger.error("No email providers configured")
            return False

        try:
            logger.info(f"Preparing verification email for user {user.userID} ({user.email})")

            # Get templates from Google Sheets
            subject_text, _ = await MessageTemplates.get_raw_template(
                'email_verification_subject',
                {
                    'firstname': user.firstname,
                    'projectName': 'JetUp'
                },
                lang=user.lang
            )

            body_html, _ = await MessageTemplates.get_raw_template(
                'email_verification_body',
                {
                    'firstname': user.firstname,
                    'verification_link': verification_link,
                    'email': user.email
                },
                lang=user.lang
            )

            logger.info(f"Templates loaded. Subject: {subject_text[:50]}...")

            # Get provider order for this email
            provider_order = self._select_provider_for_email(user.email)

            # Try to send through each provider in order
            for provider_name in provider_order:
                provider = self.providers[provider_name]
                try:
                    logger.info(f"Trying provider: {provider_name}")

                    success = await provider.send_email(
                        to=user.email,
                        subject=subject_text,
                        html_body=body_html,
                        text_body=None
                    )

                    if success:
                        logger.info(f"✅ Verification email sent successfully via {provider_name} to {user.email}")
                        return True
                    else:
                        logger.warning(f"❌ Provider {provider_name} failed to send email")

                except Exception as e:
                    logger.error(f"❌ Provider {provider_name} error: {e}")
                    continue

            # If all providers failed
            logger.error(f"❌ All email providers failed to send verification email to {user.email}")
            return False

        except Exception as e:
            logger.error(f"Error in send_verification_email: {e}")
            logger.exception("Full traceback:")
            return False

    async def send_notification_email(self, to: str, subject: str, body: str) -> bool:
        """
        Send arbitrary notification through available providers

        Args:
            to: Recipient email
            subject: Email subject
            body: Email body (HTML)

        Returns:
            bool: True if email sent successfully
        """
        if not self.providers:
            logger.error("No email providers configured")
            return False

        # Get provider order for this email
        provider_order = self._select_provider_for_email(to)

        # Try to send through each provider in order
        for provider_name in provider_order:
            provider = self.providers[provider_name]
            try:
                logger.info(f"Trying provider: {provider_name}")

                success = await provider.send_email(to, subject, body)

                if success:
                    logger.info(f"✅ Notification email sent successfully via {provider_name} to {to}")
                    return True
                else:
                    logger.warning(f"❌ Provider {provider_name} failed to send notification")

            except Exception as e:
                logger.error(f"❌ Provider {provider_name} error: {e}")
                continue

        # If all providers failed
        logger.error(f"❌ All email providers failed to send notification to {to}")
        return False

    async def test_connection(self) -> bool:
        """
        Test connection to all providers

        Returns:
            bool: True if at least one provider works
        """
        if not self.providers:
            logger.error("No email providers configured")
            return False

        any_working = False

        for provider_name, provider in self.providers.items():
            try:
                if hasattr(provider, 'test_connection'):
                    success = await provider.test_connection()
                else:
                    # For providers without test_connection method
                    success = False

                if success:
                    logger.info(f"✅ {provider_name} connection test: SUCCESS")
                    any_working = True
                else:
                    logger.warning(f"❌ {provider_name} connection test: FAILED")

            except Exception as e:
                logger.error(f"❌ {provider_name} connection test error: {e}")

        return any_working

    async def get_providers_status(self) -> Dict[str, bool]:
        """
        Return status of all providers

        Returns:
            Dict[str, bool]: Dictionary provider -> status
        """
        status = {}

        for provider_name, provider in self.providers.items():
            try:
                if hasattr(provider, 'test_connection'):
                    status[provider_name] = await provider.test_connection()
                else:
                    # For providers without test_connection method
                    status[provider_name] = False

            except Exception:
                status[provider_name] = False

        return status

    def reload_secure_domains(self):
        """Reload secure domains configuration (called after &upconfig)"""
        logger.info("Reloading secure email domains configuration...")
        self._load_secure_domains()

# Singleton instance (will be reinitialized in main.py after config loads)
email_manager = None

def init_email_manager():
    """Initialize or reinitialize email manager"""
    global email_manager
    email_manager = EmailManager()
    return email_manager

# Initial creation
email_manager = EmailManager()