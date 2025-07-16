#!/usr/bin/env python3
"""
Standalone script to test Postmark email functionality
"""

import asyncio
import os
from dotenv import load_dotenv
import aiohttp
import logging

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PostmarkTester:
    def __init__(self):
        self.api_token = os.getenv("POSTMARK_API_TOKEN")
        self.email_from = os.getenv("EMAIL_FROM", "noreply@talentir.info")
        self.email_from_name = os.getenv("EMAIL_FROM_NAME", "Talentir")

        if not self.api_token:
            raise ValueError("POSTMARK_API_TOKEN not found in environment variables")

        self.base_url = "https://api.postmarkapp.com"
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self.api_token
        }

        logger.info(f"Postmark tester initialized with token: {self.api_token[:10]}...")
        logger.info(f"Email from: {self.email_from}")

    async def test_connection(self):
        """Test connection to Postmark API"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{self.base_url}/server",
                        headers=self.headers
                ) as response:
                    if response.status == 200:
                        server_info = await response.json()
                        logger.info(f"✅ Successfully connected to Postmark")
                        logger.info(f"Server name: {server_info.get('Name')}")
                        logger.info(f"Server ID: {server_info.get('ID')}")
                        return True
                    else:
                        response_data = await response.json()
                        logger.error(f"❌ Failed to connect: {response.status}")
                        logger.error(f"Response: {response_data}")
                        return False
        except Exception as e:
            logger.error(f"❌ Connection error: {e}")
            return False

    async def send_test_email(self, to_email: str):
        """Send test email"""
        try:
            data = {
                "From": f"{self.email_from_name} <{self.email_from}>",
                "To": to_email,
                "Subject": "Talentir Test Email",
                "HtmlBody": "<h1>Тест Postmark</h1><p>Если вы видите это письмо - email работает!</p>",
                "MessageStream": "outbound"
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{self.base_url}/email",
                        headers=self.headers,
                        json=data
                ) as response:
                    response_data = await response.json()

                    if response.status == 200:
                        logger.info(f"✅ Email sent successfully to {to_email}")
                        logger.info(f"MessageID: {response_data.get('MessageID')}")
                        return True
                    else:
                        logger.error(f"❌ Failed to send email: {response.status}")
                        logger.error(f"Response: {response_data}")
                        return False

        except Exception as e:
            logger.error(f"❌ Send email error: {e}")
            return False


async def main():
    """Main test function"""
    try:
        tester = PostmarkTester()

        # Test connection
        logger.info("Testing Postmark connection...")
        if not await tester.test_connection():
            logger.error("Connection test failed")
            return

        # Test email sending
        test_email = input("Enter email address to test (or press Enter to skip): ").strip()
        if test_email:
            logger.info(f"Sending test email to {test_email}...")
            await tester.send_test_email(test_email)

    except Exception as e:
        logger.error(f"Test failed: {e}")
        logger.exception("Full traceback:")


if __name__ == "__main__":
    asyncio.run(main())