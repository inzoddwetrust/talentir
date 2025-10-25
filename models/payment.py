# bot/config/payment.py
"""
Payment model - tracks deposits and withdrawals.
"""
from sqlalchemy import Column, Integer, String, DECIMAL, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from models.base import Base, AuditMixin


class Payment(Base, AuditMixin):
    __tablename__ = 'payments'

    # Primary key
    paymentID = Column(Integer, primary_key=True, autoincrement=True)

    # Relations
    userID = Column(Integer, ForeignKey('users.userID'), nullable=False)

    # Denormalized user info for convenience
    firstname = Column(String, nullable=True)
    surname = Column(String, nullable=True)

    # Payment details
    direction = Column(String, nullable=False)  # 'in' (пополнение) или 'out' (вывод)
    amount = Column(DECIMAL(12, 2), nullable=False)  # Сумма в USD
    method = Column(String, nullable=False)  # USDT-TRC20, ETH, BNB и т.д.
    sumCurrency = Column(DECIMAL(12, 8))  # Для криптовалют нужна большая точность

    # Wallet addresses
    fromWallet = Column(String, nullable=True)  # Откуда пришла транзакция
    toWallet = Column(String, nullable=True)  # Куда отправлена

    # Transaction info
    txid = Column(String, nullable=True)  # Transaction ID в блокчейне
    status = Column(String, default="pending")  # pending, check, confirmed, rejected, cancelled

    # Confirmation
    confirmedBy = Column(String, nullable=True)  # Кто подтвердил (админ)
    confirmationTime = Column(DateTime, nullable=True)  # Когда подтвердил

    # Additional
    notes = Column(String, nullable=True)  # Заметки

    # Note: createdAt, updatedAt, ownerTelegramID, ownerEmail - от AuditMixin

    # Relationships
    user = relationship('User', backref='payments')

    def __repr__(self):
        return f"<Payment(paymentID={self.paymentID}, direction={self.direction}, amount={self.amount})>"