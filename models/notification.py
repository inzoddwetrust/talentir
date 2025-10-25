from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from models.base import Base


class Notification(Base):
    __tablename__ = 'notifications'

    notificationID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    source = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    buttons = Column(Text, nullable=True)

    targetType = Column(String, nullable=False)
    targetValue = Column(String, nullable=False)

    priority = Column(Integer, default=5)
    category = Column(String, nullable=True)
    importance = Column(String, default='normal')

    status = Column(String, default='pending')
    sentAt = Column(DateTime, nullable=True)
    failureReason = Column(Text, nullable=True)
    retryCount = Column(Integer, default=0)

    parseMode = Column(String, default='HTML')
    disablePreview = Column(Boolean, default=False)

    # Дополнительные поля из notificator
    expiryAt = Column(DateTime, nullable=True)
    silent = Column(Boolean, default=False)
    autoDelete = Column(Integer, nullable=True)


class NotificationDelivery(Base):
    __tablename__ = 'notification_deliveries'

    deliveryID = Column(Integer, primary_key=True, autoincrement=True)
    notificationID = Column(Integer, ForeignKey('notifications.notificationID'))
    userID = Column(Integer, ForeignKey('users.userID'))

    status = Column(String, default="pending")
    sentAt = Column(DateTime, nullable=True)
    attempts = Column(Integer, default=0)
    errorMessage = Column(String, nullable=True)

    notification = relationship('Notification', backref='deliveries')
    user = relationship('User', backref='notification_deliveries')