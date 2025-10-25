# models/purchase.py
from sqlalchemy import Column, Integer, String, DECIMAL, ForeignKey
from sqlalchemy.orm import relationship
from models.base import Base, AuditMixin


class Purchase(Base, AuditMixin):
    __tablename__ = 'purchases'

    # Primary key
    purchaseID = Column(Integer, primary_key=True, autoincrement=True)

    # Foreign keys
    userID = Column(Integer, ForeignKey('users.userID'), nullable=False)
    optionID = Column(Integer, ForeignKey('options.optionID'), nullable=False)

    # Project reference (БЕЗ ForeignKey из-за композитного ключа в projects)
    projectID = Column(Integer, nullable=False, index=True)
    projectName = Column(String)  # Дублируем для удобства

    # Purchase details
    packQty = Column(Integer, nullable=False)
    packPrice = Column(DECIMAL(12, 2), nullable=False)

    # Relationships
    user = relationship('User', backref='purchases')
    option = relationship('Option', back_populates='purchases')

    # НЕ создаем relationship с Project из-за композитного ключа

    def __repr__(self):
        return f"<Purchase(purchaseID={self.purchaseID}, user={self.userID}, amount={self.packPrice})>"