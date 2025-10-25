# bot/config/active_balance.py
"""
ActiveBalance model - tracks all active balance transactions.
"""
from sqlalchemy import Column, Integer, String, DECIMAL, ForeignKey
from sqlalchemy.orm import relationship
from models.base import Base, AuditMixin


class ActiveBalance(Base, AuditMixin):
    __tablename__ = 'active_balances'

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
    reason = Column(String, nullable=True)  # payment=123, transfer=456, purchase=789
    link = Column(String, nullable=True)  # Ссылка на связанную транзакцию
    notes = Column(String, nullable=True)  # Дополнительные заметки

    # Note: createdAt, updatedAt, ownerTelegramID, ownerEmail - от AuditMixin

    # Relationships
    user = relationship('User', backref='active_balance_transactions')

    def __repr__(self):
        return f"<ActiveBalance(id={self.activeBalanceID}, user={self.userID}, amount={self.amount})>"