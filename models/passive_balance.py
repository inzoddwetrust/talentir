# bot/config/passive_balance.py
"""
PassiveBalance model - tracks all passive balance transactions (bonuses, commissions).
"""
from sqlalchemy import Column, Integer, String, DECIMAL, ForeignKey
from sqlalchemy.orm import relationship
from models.base import Base, AuditMixin


class PassiveBalance(Base, AuditMixin):
    __tablename__ = 'passive_balances'

    # Primary key
    paymentID = Column(Integer, primary_key=True, autoincrement=True)

    # Relations
    userID = Column(Integer, ForeignKey('users.userID'), nullable=False)

    # Denormalized user info
    firstname = Column(String, nullable=True)
    surname = Column(String, nullable=True)

    # Transaction details
    amount = Column(DECIMAL(12, 2), nullable=False)  # Положительная или отрицательная
    status = Column(String, default='pending')  # pending, done, cancelled, error

    # Transaction metadata
    reason = Column(String, nullable=True)  # bonus=123, transfer=456, commission=789
    link = Column(String, nullable=True)  # Ссылка на связанную транзакцию
    notes = Column(String, nullable=True)  # Дополнительные заметки

    # Note: createdAt, updatedAt, ownerTelegramID, ownerEmail - от AuditMixin

    # Relationships
    user = relationship('User', backref='passive_balance_transactions')

    def __repr__(self):
        return f"<PassiveBalance(id={self.passiveBalanceID}, user={self.userID}, amount={self.amount})>"