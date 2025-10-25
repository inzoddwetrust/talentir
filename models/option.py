from sqlalchemy import Column, Integer, String, Float, Boolean
from sqlalchemy.orm import relationship
from models.base import Base


class Option(Base):
    __tablename__ = 'options'

    optionID = Column(Integer, primary_key=True)
    projectID = Column(Integer, nullable=False)  # БЕЗ ForeignKey
    projectName = Column(String)
    costPerShare = Column(Float)
    packQty = Column(Integer)
    packPrice = Column(Float)
    isActive = Column(Boolean, default=True)

    # Добавляем relationship который ожидает Purchase
    purchases = relationship('Purchase', back_populates='option')