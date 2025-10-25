# bot/config/user.py
"""
User model - central entity for the system.
"""
from sqlalchemy import Column, Integer, BigInteger, String, DECIMAL, Boolean, DateTime, Text, JSON
from datetime import datetime, timezone
from models.base import Base
from decimal import Decimal


class User(Base):
    __tablename__ = 'users'

    # Primary identification
    userID = Column(Integer, primary_key=True, autoincrement=True)
    telegramID = Column(BigInteger, unique=True, nullable=False)
    upline = Column(BigInteger, nullable=True)  # TelegramID спонсора
    createdAt = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Personal information (остаются как отдельные поля для быстрого доступа)
    email = Column(String, nullable=True)
    firstname = Column(String, nullable=True)
    surname = Column(String, nullable=True)
    birthday = Column(String, nullable=True)
    address = Column(String, nullable=True)
    phoneNumber = Column(String, nullable=True)
    city = Column(String, nullable=True)
    country = Column(String, nullable=True)
    passport = Column(String, nullable=True)

    # System fields
    lang = Column(String, default="en")  # Язык остается отдельным полем
    status = Column(String, default="active")  # active, blocked, deleted
    lastActive = Column(DateTime, nullable=True)

    # Balances
    balanceActive = Column(DECIMAL(12, 2), default=0.0)
    balancePassive = Column(DECIMAL(12, 2), default=0.0)

    # MLM System - критичные поля для производительности
    rank = Column(String, default="start", index=True)  # start, builder, growth, leadership, director
    isActive = Column(Boolean, default=False, index=True)  # Активен в текущем месяце (PV >= 200)
    teamVolumeTotal = Column(DECIMAL(12, 2), default=0.0)

    # MLM детали в JSON
    mlmStatus = Column(JSON, nullable=True)
    # {
    #   "rankQualifiedAt": null,
    #   "assignedRank": null,
    #   "isFounder": false,
    #   "lastActiveMonth": null,
    #   "pioneerPurchasesCount": 0,
    #   "hasPioneerBonus": false
    # }

    mlmVolumes = Column(JSON, nullable=True)
    # {
    #   "personalTotal": 0.0,      # Накопительный личный объем
    #   "monthlyPV": 0.0,          # PV текущего месяца
    #   "autoship": {"enabled": false, "amount": 200}
    # }

    # Structured JSON fields
    personalData = Column(JSON, nullable=True)
    # {
    #   "eulaAccepted": true,
    #   "eulaVersion": "1.0",
    #   "eulaAcceptedAt": "2024-01-01T10:00:00",
    #   "dataFilled": false,  # бывший isFilled
    #   "kyc": {
    #     "status": "not_started",  # not_started, pending, verified, rejected
    #     "verifiedAt": null,
    #     "documents": [],
    #     "level": 0
    #   }
    # }

    emailVerification = Column(JSON, nullable=True)
    # {
    #   "confirmed": false,
    #   "token": "UCfwYV7sNTu8p4X7",
    #   "sentAt": "2024-01-15T10:30:00",
    #   "confirmedAt": null,
    #   "attempts": 1
    # }

    settings = Column(JSON, nullable=True)
    # {
    #   "strategy": "risky",  # manual, safe, aggressive, risky
    #   "notifications": {"bonus": true, "purchase": true},
    #   "display": {"showBalance": true}
    # }

    notes = Column(Text, nullable=True)  # Только для админских заметок
    stateFSM = Column(String, nullable=True)  # FSM state (оставляем для совместимости)

    # Class methods
    @classmethod
    def create_from_telegram_data(cls, session, telegram_user):
        """
        Создаёт нового пользователя или возвращает существующего из базы данных на основе данных Telegram.
        Если таблица пользователей пуста, создает первого пользователя с ID=1
        """
        user = session.query(cls).filter_by(telegramID=telegram_user.id).first()
        if not user:
            # Проверяем, есть ли вообще записи в таблице
            users_exist = session.query(cls).first() is not None

            if not users_exist:
                # Если таблица пуста, создаем первого пользователя с ID=1
                new_user_id = 1
            else:
                # Назначаем следующий ID по порядку
                max_user_id = session.query(cls).order_by(cls.userID.desc()).first()
                new_user_id = max_user_id.userID + 1

            user = cls(
                userID=new_user_id,
                telegramID=telegram_user.id,
                lang=telegram_user.language_code,
                firstname=telegram_user.first_name,
                surname=telegram_user.last_name or None
            )
            session.add(user)
            session.commit()
        return user

    # Properties для обратной совместимости
    @property
    def isFilled(self):
        """Обратная совместимость для проверки заполненности данных"""
        if self.personalData:
            return self.personalData.get('dataFilled', False)
        return False

    @isFilled.setter
    def isFilled(self, value):
        """Сеттер для обратной совместимости"""
        if not self.personalData:
            self.personalData = {}
        self.personalData['dataFilled'] = value

    @property
    def kyc(self):
        """Обратная совместимость для проверки KYC"""
        if self.personalData:
            kyc_data = self.personalData.get('kyc', {})
            return kyc_data.get('status') == 'verified'
        return False

    @kyc.setter
    def kyc(self, value):
        """Сеттер для обратной совместимости"""
        if not self.personalData:
            self.personalData = {'kyc': {}}
        if 'kyc' not in self.personalData:
            self.personalData['kyc'] = {}

        if value:
            self.personalData['kyc']['status'] = 'verified'
        else:
            self.personalData['kyc']['status'] = 'not_started'

    def __repr__(self):
        return f"<User(userID={self.userID}, telegram={self.telegramID}, rank={self.rank})>"

    @property
    def isFilled(self):
        """Обратная совместимость - проверяем dataFilled в personalData"""
        if not self.personalData:
            return False
        return self.personalData.get('dataFilled', False)

    @isFilled.setter
    def isFilled(self, value):
        """Обратная совместимость - устанавливаем dataFilled в personalData"""
        if not self.personalData:
            self.personalData = {}
        self.personalData['dataFilled'] = value

    @property
    def emailConfirmed(self):
        """Проверка подтверждения email"""
        if not self.emailVerification:
            return False
        confirmed = self.emailVerification.get('confirmed', False)
        # Обрабатываем разные представления True для обратной совместимости
        return confirmed in [True, 1, '1', 'true', 'True']

    @emailConfirmed.setter
    def emailConfirmed(self, value):
        """Установка статуса подтверждения email"""
        if not self.emailVerification:
            self.emailVerification = {}
        self.emailVerification['confirmed'] = bool(value)
        if value:
            self.emailVerification['confirmedAt'] = datetime.now(timezone.utc).isoformat()

    # === PROPERTY для настроек стратегии (для старого кода) ===
    @property
    def strategy(self):
        """Получение стратегии пользователя"""
        if not self.settings:
            return 'manual'  # дефолтная стратегия
        return self.settings.get('strategy', 'manual')

    @strategy.setter
    def strategy(self, value):
        """Установка стратегии"""
        if not self.settings:
            self.settings = {}
        self.settings['strategy'] = value

    # === PROPERTY для pioneer статуса ===
    @property
    def isPioneer(self):
        """Проверка pioneer статуса"""
        if not self.mlmStatus:
            return False
        return self.mlmStatus.get('isFounder', False)

    @isPioneer.setter
    def isPioneer(self, value):
        """Установка pioneer статуса"""
        if not self.mlmStatus:
            self.mlmStatus = {}
        self.mlmStatus['isFounder'] = bool(value)

    # === PROPERTY для месячного PV ===
    @property
    def monthlyPV(self):
        """Получение PV текущего месяца"""
        if not self.mlmVolumes:
            return Decimal('0.0')
        return Decimal(str(self.mlmVolumes.get('monthlyPV', 0)))

    @monthlyPV.setter
    def monthlyPV(self, value):
        """Установка PV текущего месяца"""
        if not self.mlmVolumes:
            self.mlmVolumes = {}
        self.mlmVolumes['monthlyPV'] = float(value)

    # === PROPERTY для личного объема ===
    @property
    def personalVolume(self):
        """Получение накопительного личного объема"""
        if not self.mlmVolumes:
            return Decimal('0.0')
        return Decimal(str(self.mlmVolumes.get('personalTotal', 0)))

    @personalVolume.setter
    def personalVolume(self, value):
        """Установка накопительного личного объема"""
        if not self.mlmVolumes:
            self.mlmVolumes = {}
        self.mlmVolumes['personalTotal'] = float(value)

    # === HELPER методы для удобства ===
    def has_filled_data(self):
        """Проверка заполнены ли все персональные данные"""
        required_fields = ['firstname', 'surname', 'email', 'phoneNumber',
                           'country', 'city', 'address', 'birthday', 'passport']
        return all(getattr(self, field) for field in required_fields)

    def needs_email_verification(self):
        """Проверка нужна ли верификация email"""
        return self.isFilled and not self.emailConfirmed

    def can_make_purchases(self):
        """Может ли пользователь совершать покупки"""
        return self.isFilled and self.emailConfirmed

    def get_verification_token(self):
        """Получение токена верификации"""
        if not self.emailVerification:
            return None
        return self.emailVerification.get('token')

    def set_verification_token(self, token):
        """Установка токена верификации"""
        if not self.emailVerification:
            self.emailVerification = {}
        self.emailVerification['token'] = token
        self.emailVerification['confirmed'] = False
        self.emailVerification['sentAt'] = datetime.now(timezone.utc).isoformat()

    def mark_email_verified(self):
        """Отметка email как подтвержденного"""
        if not self.emailVerification:
            self.emailVerification = {}
        self.emailVerification['confirmed'] = True
        self.emailVerification['confirmedAt'] = datetime.now(timezone.utc).isoformat()

    def get_email_attempts(self):
        """Получение количества попыток отправки email"""
        if not self.emailVerification:
            return 0
        return self.emailVerification.get('attempts', 0)

    def increment_email_attempts(self):
        """Увеличение счетчика попыток отправки email"""
        if not self.emailVerification:
            self.emailVerification = {}
        self.emailVerification['attempts'] = self.emailVerification.get('attempts', 0) + 1