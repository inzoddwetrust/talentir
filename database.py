from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean, PrimaryKeyConstraint
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.declarative import declarative_base
import datetime
from random import randint

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'

    userID = Column(Integer, primary_key=True)
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)
    upline = Column(Integer, ForeignKey('users.telegramID'), nullable=True)
    lang = Column(String)
    firstname = Column(String)
    surname = Column(String, nullable=True)
    birthday = Column(DateTime, nullable=True)
    address = Column(String, nullable=True)
    phoneNumber = Column(String, nullable=True)
    country = Column(String, nullable=True)
    passport = Column(String, nullable=True)  # New field
    city = Column(String, nullable=True)
    telegramID = Column(Integer, unique=True, nullable=False)
    email = Column(String, nullable=True)
    balanceActive = Column(Float, default=0.00)
    balancePassive = Column(Float, default=0.00)
    isFilled = Column(Boolean, default=False)
    kyc = Column(Boolean, default=False)
    lastActive = Column(DateTime, nullable=True)
    status = Column(String, default="active")
    notes = Column(Text, nullable=True)
    settings = Column(String, nullable=True)

    referrals = relationship('User', backref=backref('referrer', remote_side=[telegramID]))

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
                # Иначе генерируем ID как раньше
                max_user_id = session.query(cls).order_by(cls.userID.desc()).first()
                new_user_id = (max_user_id.userID if max_user_id else 0) + randint(1, 42)

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


class Project(Base):
    __tablename__ = 'projects'

    projectID = Column(Integer, primary_key=False)
    lang = Column(String, nullable=False)
    projectName = Column(String, nullable=False)
    projectTitle = Column(String, nullable=False)
    fullText = Column(Text)
    status = Column(String)
    rate = Column(Float)
    linkImage = Column(String)
    linkPres = Column(String)
    linkVideo = Column(String)
    docsFolder = Column(String)

    __table_args__ = (
        PrimaryKeyConstraint('projectID', 'lang'),
    )


class Option(Base):
    __tablename__ = 'options'

    optionID = Column(Integer, primary_key=True, autoincrement=True)
    projectID = Column(Integer, ForeignKey('projects.projectID'))
    projectName = Column(String, nullable=False)
    costPerShare = Column(Float, nullable=False)
    packQty = Column(Integer, nullable=False)
    packPrice = Column(Float, nullable=False)
    isActive = Column(Boolean, default=True)


class Purchase(Base):
    __tablename__ = 'purchases'

    purchaseID = Column(Integer, primary_key=True, autoincrement=True)  # Уникальный идентификатор покупки
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)
    userID = Column(Integer, ForeignKey('users.userID'))  # Идентификатор пользователя, совершившего покупку
    projectID = Column(Integer, ForeignKey('projects.projectID'))  # Идентификатор проекта, к которому относится покупка
    projectName = Column(String, nullable=False)  # Название проекта
    optionID = Column(Integer, ForeignKey('options.optionID'))  # Идентификатор опциона акций
    packQty = Column(Integer, nullable=False)  # Количество акций в пакете
    packPrice = Column(Float, nullable=False)  # Итоговая стоимость покупки

    user = relationship('User', backref='purchases')
    project = relationship('Project', backref='purchases')
    option = relationship('Option', backref='purchases')


class Bonus(Base):
    __tablename__ = 'bonuses'

    bonusID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)

    userID = Column(Integer, ForeignKey('users.userID'), nullable=False)
    downlineID = Column(Integer, ForeignKey('users.userID'), nullable=True)  # Nullable для системных бонусов
    purchaseID = Column(Integer, ForeignKey('purchases.purchaseID'), nullable=True)  # Опциональная связь

    projectID = Column(Integer, nullable=True)
    optionID = Column(Integer, nullable=True)
    packQty = Column(Integer, nullable=True)
    packPrice = Column(Float, nullable=True)

    uplineLevel = Column(Integer, nullable=True)  # Уровень реферала (может быть null для системных бонусов)
    bonusRate = Column(Float, nullable=False)  # Ставка бонуса
    bonusAmount = Column(Float, nullable=False)  # Сумма бонуса

    status = Column(String, default="pending")  # pending, processing, paid, cancelled, error
    notes = Column(Text, nullable=True)  # Служебные заметки

    user = relationship('User', foreign_keys=[userID], backref='received_bonuses')
    downline = relationship('User', foreign_keys=[downlineID], backref='generated_bonuses')
    purchase = relationship('Purchase', backref='bonuses')


class Payment(Base):
    __tablename__ = 'payments'

    paymentID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)
    userID = Column(Integer, ForeignKey('users.userID'))
    firstname = Column(String, nullable=False)
    surname = Column(String, nullable=True)
    direction = Column(String, nullable=False, default='incoming')  # 'incoming' или 'outgoing'
    amount = Column(Float, nullable=False)
    method = Column(String, nullable=False)
    fromWallet = Column(String, nullable=True)
    toWallet = Column(String, nullable=True)
    txid = Column(String, unique=True, nullable=True)
    sumCurrency = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    confirmedBy = Column(String, nullable=True)
    confirmationTime = Column(DateTime, nullable=True)

    user = relationship('User', backref='payments')


class ActiveBalance(Base):
    __tablename__ = 'active_balance'

    paymentID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)
    userID = Column(Integer, ForeignKey('users.userID'))
    firstname = Column(String, nullable=False)
    surname = Column(String, nullable=True)
    amount = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    reason = Column(String, nullable=False)
    link = Column(String, nullable=True)
    notes = Column(Text, nullable=True)

    user = relationship('User', backref='active_balance_records')


class PassiveBalance(Base):
    __tablename__ = 'passive_balance'

    paymentID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)
    userID = Column(Integer, ForeignKey('users.userID'))
    firstname = Column(String, nullable=False)
    surname = Column(String, nullable=True)
    amount = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    reason = Column(String, nullable=False)
    link = Column(String, nullable=True)
    notes = Column(Text, nullable=True)

    user = relationship('User', backref='passive_balance_records')


class Transfer(Base):
    __tablename__ = 'transfers'

    transferID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)
    senderUserID = Column(Integer, ForeignKey('users.userID'))
    senderFirstname = Column(String, nullable=False)
    senderSurname = Column(String, nullable=True)
    fromBalance = Column(String, nullable=False)  # 'active' или 'passive'
    amount = Column(Float, nullable=False)
    recieverUserID = Column(Integer, ForeignKey('users.userID'))
    receiverFirstname = Column(String, nullable=False)
    receiverSurname = Column(String, nullable=True)
    toBalance = Column(String, nullable=False)  # 'active' или 'passive'
    status = Column(String, nullable=False)
    notes = Column(Text, nullable=True)

    sender = relationship('User', foreign_keys=[senderUserID], backref='sent_transfers')
    receiver = relationship('User', foreign_keys=[recieverUserID], backref='received_transfers')


class Notification(Base):
    __tablename__ = 'notifications'

    notificationID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=datetime.datetime.utcnow)
    source = Column(String, nullable=False)  # Источник уведомления
    text = Column(String, nullable=False)  # Текст уведомления
    target_type = Column(String, nullable=False)  # user, group, all, filter
    target_value = Column(String, nullable=True)  # userID, groupID, NULL для all, JSON с условиями для filter
    priority = Column(Integer, default=1)  # Приоритет обработки (1 - обычный, 2 - высокий и т.д.)

    buttons = Column(String, nullable=True)  # Список списков в формате [["callback1:Кнопка 1", "callback2:Кнопка 2"]]
    parse_mode = Column(String, default="HTML")  # HTML, Markdown
    disable_web_page_preview = Column(Boolean, default=True)

    expiry_at = Column(DateTime, nullable=True)  # Когда уведомление становится неактуальным
    category = Column(String, nullable=True)  # system, payment, marketing, etc.
    importance = Column(String, default="normal")  # critical, high, normal, low - для UI

    silent = Column(Boolean, default=False)  # Отправлять ли без звука
    auto_delete = Column(Integer, nullable=True)  # Через сколько секунд удалить
    requires_confirmation = Column(Boolean, default=False)  # Требует ли подтверждения прочтения

    parent_id = Column(Integer, ForeignKey('notifications.notificationID'), nullable=True)
    thread_id = Column(String, nullable=True)  # Для группировки связанных уведомлений
    related_entity_type = Column(String, nullable=True)
    related_entity_id = Column(Integer, nullable=True)

    deliveries = relationship("NotificationDelivery", backref="notification")


class NotificationDelivery(Base):
    __tablename__ = 'notification_deliveries'

    deliveryID = Column(Integer, primary_key=True, autoincrement=True)
    notificationID = Column(Integer, ForeignKey('notifications.notificationID'))
    userID = Column(Integer, ForeignKey('users.userID'))
    status = Column(String, default="pending")
    sent_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, default=0)
    error_message = Column(String, nullable=True)

    user = relationship('User', backref='notification_deliveries')
