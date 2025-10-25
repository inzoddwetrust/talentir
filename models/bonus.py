# bot/config/bonus.py
"""
Bonus model - tracks all referral commissions and bonuses.
"""
from sqlalchemy import Column, Integer, String, Float, DECIMAL, Text, ForeignKey
from sqlalchemy.orm import relationship
from models.base import Base, AuditMixin


class Bonus(Base, AuditMixin):
    __tablename__ = 'bonuses'

    # Primary key
    bonusID = Column(Integer, primary_key=True, autoincrement=True)

    # Relations
    userID = Column(Integer, ForeignKey('users.userID'), nullable=False)  # Кто получает бонус
    downlineID = Column(Integer, ForeignKey('users.userID'),
                        nullable=True)  # От кого (может быть null для системных бонусов)
    purchaseID = Column(Integer, ForeignKey('purchases.purchaseID'), nullable=True)  # За какую покупку

    # Denormalized data for reports
    projectID = Column(Integer, nullable=True)
    optionID = Column(Integer, nullable=True)
    packQty = Column(Integer, nullable=True)
    packPrice = Column(DECIMAL(12, 2), nullable=True)

    # MLM Commission details
    commissionType = Column(String, nullable=True)  # differential, referral, pioneer, global_pool
    uplineLevel = Column(Integer, nullable=True)  # Уровень в структуре (1, 2, 3...)
    fromRank = Column(String, nullable=True)  # Ранг получателя бонуса
    sourceRank = Column(String, nullable=True)  # Ранг источника (для дифференциала)

    # Bonus calculation
    bonusRate = Column(Float, nullable=False)  # Процент комиссии (0.04 для 4%)
    bonusAmount = Column(DECIMAL(12, 2), nullable=False)  # Сумма бонуса
    compressionApplied = Column(Integer, default=0)  # Было ли сжатие (0/1 для SQLite)

    # Status
    status = Column(String, default="pending")  # pending, processing, paid, cancelled, error
    notes = Column(Text, nullable=True)  # Служебные заметки

    # Note: createdAt, updatedAt, ownerTelegramID, ownerEmail - от AuditMixin

    # Relationships
    user = relationship('User', foreign_keys=[userID], backref='bonuses_received')
    downline = relationship('User', foreign_keys=[downlineID], backref='bonuses_generated')
    purchase = relationship('Purchase', backref='bonuses')

    def __repr__(self):
        return f"<Bonus(bonusID={self.bonusID}, user={self.userID}, amount={self.bonusAmount})>"