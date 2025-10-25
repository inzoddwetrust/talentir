# bot/config/base.py
"""
Base model and mixins for all database tables.
"""
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, BigInteger, String, DateTime
from datetime import datetime, timezone

Base = declarative_base()


class AuditMixin:
    ownerTelegramID = Column(BigInteger, nullable=True, index=True)
    ownerEmail = Column(String, nullable=True, index=True)

    createdAt = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updatedAt = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                       onupdate=lambda: datetime.now(timezone.utc))