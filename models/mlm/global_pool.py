# bot/config/mlm/global_pool.py
"""
GlobalPool model - global pool calculations and distributions.
"""
from sqlalchemy import Column, Integer, String, DECIMAL, DateTime, Text
from datetime import datetime, timezone
from models.base import Base


class GlobalPool(Base):
    __tablename__ = 'global_pool'

    poolID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Period
    month = Column(String, nullable=False)  # "2024-01" format

    # Pool calculation
    totalCompanyVolume = Column(DECIMAL(15, 2), nullable=False)  # Общий оборот
    poolPercentage = Column(DECIMAL(5, 4), default=0.02)  # 2% = 0.0200
    poolSize = Column(DECIMAL(12, 2), nullable=False)  # 2% от оборота

    # Distribution
    qualifiedUsersCount = Column(Integer, nullable=False)  # Количество квалифицированных
    perUserAmount = Column(DECIMAL(12, 2), nullable=True)  # Сумма на каждого

    # Status
    status = Column(String, default='calculated')  # calculated, distributed, cancelled
    distributedAt = Column(DateTime, nullable=True)

    # Qualified users list
    qualifiedUsers = Column(Text, nullable=True)  # JSON array of userIDs

    def __repr__(self):
        return f"<GlobalPool(month={self.month}, size={self.poolSize}, users={self.qualifiedUsersCount})>"