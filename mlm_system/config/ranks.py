# bot/mlm_system/config/ranks.py
"""
MLM ranks configuration and constants.
"""
from enum import Enum
from decimal import Decimal


class Rank(Enum):
    START = "start"
    BUILDER = "builder"
    GROWTH = "growth"
    LEADERSHIP = "leadership"
    DIRECTOR = "director"


RANK_CONFIG = {
    Rank.START: {
        "percentage": Decimal("0.04"),  # 4%
        "teamVolumeRequired": Decimal("0"),
        "activePartnersRequired": 0,
        "displayName": "Старт"
    },
    Rank.BUILDER: {
        "percentage": Decimal("0.08"),  # 8%
        "teamVolumeRequired": Decimal("50000"),
        "activePartnersRequired": 2,
        "displayName": "Строитель"
    },
    Rank.GROWTH: {
        "percentage": Decimal("0.12"),  # 12%
        "teamVolumeRequired": Decimal("250000"),
        "activePartnersRequired": 5,
        "displayName": "Рост"
    },
    Rank.LEADERSHIP: {
        "percentage": Decimal("0.15"),  # 15%
        "teamVolumeRequired": Decimal("1000000"),
        "activePartnersRequired": 10,
        "displayName": "Лидерство"
    },
    Rank.DIRECTOR: {
        "percentage": Decimal("0.18"),  # 18%
        "teamVolumeRequired": Decimal("5000000"),
        "activePartnersRequired": 15,
        "displayName": "Директор"
    }
}

# Constants
MINIMUM_PV = Decimal("200")  # Минимальный PV для активации
PIONEER_BONUS_PERCENTAGE = Decimal("0.04")  # +4% для первых 50
REFERRAL_BONUS_PERCENTAGE = Decimal("0.01")  # 1% за привлечение
GLOBAL_POOL_PERCENTAGE = Decimal("0.02")  # 2% от оборота
TRANSFER_BONUS_PERCENTAGE = Decimal("0.02")  # 2% бонус при переводе с passive

# Thresholds
PIONEER_MAX_COUNT = 50  # Первые 50 покупок в структуре
REFERRAL_BONUS_MIN_AMOUNT = Decimal("5000")  # Минимум для referral bonus