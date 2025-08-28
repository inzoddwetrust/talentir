import logging
import aiosmtplib
from typing import Dict, Any, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import config
from templates import MessageTemplates

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
                message['From'] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
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
                message['From'] = f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>"
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
            smtp = aiosmtplib.SMTP(hostname=self.smtp_host, port=self.smtp_port)
            await smtp.connect()
            await smtp.starttls()
            await smtp.login(self.username, self.password)
            await smtp.quit()

            logger.info("SMTP connection test successful")
            return True

        except Exception as e:
            logger.error(f"SMTP connection test failed: {e}")
            return False


class EmailManager:
    """Manager for email operations through multiple providers"""

    def __init__(self):
        self.providers = []

        # SMTP as main provider
        if all([
            hasattr(config, 'SMTP_HOST') and config.SMTP_HOST,
            hasattr(config, 'SMTP_USER') and config.SMTP_USER,
            hasattr(config, 'SMTP_PASSWORD') and config.SMTP_PASSWORD
        ]):
            smtp_port = getattr(config, 'SMTP_PORT', 587)
            self.providers.append(SMTPProvider(
                config.SMTP_HOST,
                smtp_port,
                config.SMTP_USER,
                config.SMTP_PASSWORD
            ))
            logger.info(f"EmailManager: Added SMTP provider ({config.SMTP_HOST}:{smtp_port})")

        # Future providers can be added here
        # Example:
        # if hasattr(config, 'ANOTHER_PROVIDER_KEY'):
        #     self.providers.append(AnotherProvider(...))

        if not self.providers:
            logger.warning("EmailManager: No email providers configured")

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
                    'projectName': 'Talentir'
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

            # Try to send through each provider in order
            for i, provider in enumerate(self.providers):
                try:
                    provider_name = provider.__class__.__name__
                    logger.info(f"Trying provider {i + 1}/{len(self.providers)}: {provider_name}")

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
                    provider_name = provider.__class__.__name__
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

        # Try to send through each provider in order
        for i, provider in enumerate(self.providers):
            try:
                provider_name = provider.__class__.__name__
                logger.info(f"Trying provider {i + 1}/{len(self.providers)}: {provider_name}")

                success = await provider.send_email(to, subject, body)

                if success:
                    logger.info(f"✅ Notification email sent successfully via {provider_name} to {to}")
                    return True
                else:
                    logger.warning(f"❌ Provider {provider_name} failed to send notification")

            except Exception as e:
                provider_name = provider.__class__.__name__
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

        for provider in self.providers:
            provider_name = provider.__class__.__name__

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

        for provider in self.providers:
            provider_name = provider.__class__.__name__

            try:
                if hasattr(provider, 'test_connection'):
                    status[provider_name] = await provider.test_connection()
                else:
                    # For providers without test_connection method
                    status[provider_name] = False

            except Exception:
                status[provider_name] = False

        return status


# Singleton instance
email_manager = EmailManager()