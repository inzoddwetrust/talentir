import os
from dotenv import load_dotenv
from txid_checker import TxidValidationCode

# Загрузка .env
load_dotenv()

# База данных
DATABASE_URL = os.getenv("DATABASE_URL")

# Google API
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]
# Legacy migration sheet
LEGACY_SHEET_ID = "1mbaRSbOs0Hc98iJ3YnZnyqL5yxeSuPJCef5PFjPHpFg"

# Telegram
API_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
ADMINS = list(map(int, os.getenv("ADMINS").split(",")))
ADMIN_USER_IDS = [45, 33, 1]  # Будет заполнено при инициализации
ADMIN_LINKS = ['@jetup', '@iamshangtsung']  # Будет заполнено при инициализации

# Кошельки и платежи
WALLET_TRC = os.getenv("WALLET_TRC")
WALLET_ETH = os.getenv("WALLET_ETH")
WALLETS = {
    "USDT-TRC20": WALLET_TRC,
    "TRX": WALLET_TRC,
    "ETH": WALLET_ETH,
    "BNB": WALLET_ETH,
    "USDT-BSC20": WALLET_ETH,
    "USDT-ERC20": WALLET_ETH
}

STABLECOINS = ["USDT-ERC20", "USDT-BSC20", "USDT-TRC20"]

TX_BROWSERS = {
    "ETH": "|url|etherscan.io/tx/",
    "BNB": "|url|bscscan.com/tx/",
    "USDT-ERC20": "|url|etherscan.io/tx/",
    "USDT-BSC20": "|url|bscscan.com/tx/",
    "TRX": "|url|tronscan.org/#/transaction/",
    "USDT-TRC20": "|url|tronscan.org/#/transaction/"
}

# API ключи
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY")
TRON_API_KEY = os.getenv("TRON_API_KEY")

# Прочие настройки
DEFAULT_REFERRER_ID = int(os.getenv("DEFAULT_REFERRER_ID", "0"))

# Рефералка
PURCHASE_BONUSES = {
    "level_1": 10,
    "level_2": 4,
    "level_3": 2,
    "level_4": 1,
    "level_5": 1,
    "level_6": 1,
}

# Коэффициенты оценки портфеля в зависимости от стратегии
STRATEGY_COEFFICIENTS = {
    "manual": 1.0,
    "safe": 4.50,
    "aggressive": 11.00,
    "risky": 25.00
}

# Transfer Bonus

TRANSFER_BONUS = 2

# Ошибки
TXID_TEMPLATE_MAPPING = {
    TxidValidationCode.VALID_TRANSACTION: 'txid_success',
    TxidValidationCode.INVALID_PREFIX: 'txid_invalid_format',
    TxidValidationCode.INVALID_LENGTH: 'txid_invalid_format',
    TxidValidationCode.INVALID_CHARS: 'txid_invalid_format',
    TxidValidationCode.UNSUPPORTED_METHOD: 'txid_unsupported_method',
    TxidValidationCode.TRANSACTION_NOT_FOUND: 'txid_not_found',
    TxidValidationCode.WRONG_RECIPIENT: 'txid_wrong_recipient',
    TxidValidationCode.WRONG_NETWORK: 'txid_wrong_network',
    TxidValidationCode.API_ERROR: 'txid_api_error',
    TxidValidationCode.TXID_ALREADY_USED: 'txid_already_used',
    TxidValidationCode.NEEDS_CONFIRMATION: 'txid_needs_confirmation'
}

# Социальные сети проекта
SOCIAL_LINKS = {
    'telegram_link': 'https://t.me/project_channel',
    'telegram_name': '@project_channel',
    'twitter_link': 'https://twitter.com/project_twitter',
    'twitter_name': '@project_twitter',
    'instagram_link': 'https://instagram.com/project_instagram',
    'instagram_name': '@project_instagram',
    'linkedin_link': 'https://linkedin.com/company/project_company',
    'linkedin_name': 'Project Company',
    'facebook_link': 'https://facebook.com/project_page',
    'facebook_name': 'Project Page'
}

# URL для FAQ веб-приложения
FAQ_URL = "91.227.18.8/books/user-documents/page/option-alienation-agreement"

# Список обязательных каналов/групп для подписки
REQUIRED_CHANNELS = [
    {
        "chat_id": "@jetnews_en",
        "title": "JET News English",
        "url": "https://t.me/jetnews_en",
        "lang": "en"
    },
    {
        "chat_id": "@jetnews_ru",
        "title": "JET News Русский",
        "url": "https://t.me/jetnews_ru",
        "lang": "ru"
    },
    {
        "chat_id": "@jetnews_de",
        "title": "JET News Deutsch",
        "url": "https://t.me/jetnews_de",
        "lang": "de"
    },
    {
        "chat_id": "@jetnews_in",
        "title": "JET News Indonesia",
        "url": "https://t.me/jetnews_in",
        "lang": "in"
    }
]

# BookStack API
BOOKSTACK_URL = os.getenv("BOOKSTACK_URL", "https:/jetup.info")
BOOKSTACK_TOKEN_ID = os.getenv("BOOKSTACK_TOKEN_ID")
BOOKSTACK_TOKEN_SECRET = os.getenv("BOOKSTACK_TOKEN_SECRET")

# Стандартные документы проектов
PROJECT_DOCUMENTS = {
    "agreement": "option-alienation-agreement",
    "cert": "option-certificate",
    "whitepaper": "project-whitepaper",
    "roadmap": "project-roadmap",
    "team": "project-team",
    "faq": "project-faq"
}

SMTP_HOST = os.getenv("SMTP_HOST", "mail.talentir.info")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "noreply@talentir.info")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Email settings для Postmark
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@talentir.info")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Talentir")