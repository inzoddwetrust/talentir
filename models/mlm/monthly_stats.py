# bot/config/mlm/monthly_stats.py
"""
MonthlyStats model - monthly MLM statistics snapshot.
"""
from sqlalchemy import Column, Integer, String, DECIMAL, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from models.base import Base


class MonthlyStats(Base):
    __tablename__ = 'monthly_stats'

    statsID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relations
    userID = Column(Integer, ForeignKey('users.userID'), nullable=False)

    # Period
    month = Column(String, nullable=False)  # "2024-01" format

    # Volumes
    personalVolume = Column(DECIMAL(12, 2), default=0)
    teamVolume = Column(DECIMAL(12, 2), default=0)

    # Activity
    activePartnersCount = Column(Integer, default=0)
    directReferralsCount = Column(Integer, default=0)
    totalTeamSize = Column(Integer, default=0)

    # Rank and earnings
    activeRank = Column(String, nullable=True)  # Ранг в этом месяце
    commissionsEarned = Column(DECIMAL(12, 2), default=0)
    bonusesEarned = Column(DECIMAL(12, 2), default=0)
    globalPoolEarned = Column(DECIMAL(12, 2), default=0)

    # Status
    wasActive = Column(Integer, default=0)  # Boolean as Integer for SQLite

    # Relationships
    user = relationship('User', backref='monthly_stats')

    def __repr__(self):
        return f"<MonthlyStats(user={self.userID}, month={self.month}, pv={self.personalVolume})>"