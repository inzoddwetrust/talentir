# bot/config/transfer.py
"""
Transfer model - tracks internal transfers between users.
"""
from sqlalchemy import Column, Integer, String, DECIMAL, ForeignKey
from sqlalchemy.orm import relationship
from models.base import Base, AuditMixin


class Transfer(Base, AuditMixin):
    __tablename__ = 'transfers'

    # Primary key
    transferID = Column(Integer, primary_key=True, autoincrement=True)

    # Sender info
    senderUserID = Column(Integer, ForeignKey('users.userID'), nullable=False)
    senderFirstname = Column(String, nullable=True)
    senderSurname = Column(String, nullable=True)
    fromBalance = Column(String, nullable=False)  # 'active' или 'passive'

    # Transfer details
    amount = Column(DECIMAL(12, 2), nullable=False)

    # Receiver info
    receiverUserID = Column(Integer, ForeignKey('users.userID'), nullable=False)
    receiverFirstname = Column(String, nullable=True)
    receiverSurname = Column(String, nullable=True)
    toBalance = Column(String, nullable=False)  # 'active' или 'passive'

    # Status
    status = Column(String, default="pending")  # pending, completed, cancelled, error
    notes = Column(String, nullable=True)

    # Note: createdAt, updatedAt, ownerTelegramID, ownerEmail - от AuditMixin
    # ownerTelegramID будет равен senderUserID's telegramID

    # Relationships
    sender = relationship('User', foreign_keys=[senderUserID], backref='transfers_sent')
    receiver = relationship('User', foreign_keys=[receiverUserID], backref='transfers_received')

    def __repr__(self):
        return f"<Transfer(transferID={self.transferID}, from={self.senderUserID}, to={self.receiverUserID}, amount={self.amount})>"