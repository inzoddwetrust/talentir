# bot/config/mlm/system_time.py
"""
SystemTime model - virtual time for testing.
"""
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from datetime import datetime, timezone
from models.base import Base


class SystemTime(Base):
    __tablename__ = 'system_time'

    timeID = Column(Integer, primary_key=True, autoincrement=True)

    # Time settings
    realTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    virtualTime = Column(DateTime, nullable=True)
    isTestMode = Column(Boolean, default=False)

    # Metadata
    createdBy = Column(Integer, nullable=True)  # Admin userID
    notes = Column(String, nullable=True)  # "Testing Grace Day", etc.

    def __repr__(self):
        return f"<SystemTime(test={self.isTestMode}, virtual={self.virtualTime})>"