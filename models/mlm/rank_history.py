# bot/config/mlm/rank_history.py
"""
RankHistory model - tracks rank achievements and changes.
"""
from sqlalchemy import Column, Integer, String, DECIMAL, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from models.base import Base


class RankHistory(Base):
    __tablename__ = 'rank_history'

    historyID = Column(Integer, primary_key=True, autoincrement=True)
    createdAt = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relations
    userID = Column(Integer, ForeignKey('users.userID'), nullable=False)

    # Rank details
    previousRank = Column(String, nullable=True)
    newRank = Column(String, nullable=False)

    # Qualification metrics at time of achievement
    teamVolume = Column(DECIMAL(12, 2), nullable=True)
    activePartners = Column(Integer, nullable=True)
    qualificationMethod = Column(String, nullable=True)  # natural, assigned, promotion

    # If assigned by founder
    assignedBy = Column(Integer, ForeignKey('users.userID'), nullable=True)

    # Additional context
    notes = Column(Text, nullable=True)  # JSON with additional data

    # Relationships
    user = relationship('User', foreign_keys=[userID], backref='rank_history')
    assigner = relationship('User', foreign_keys=[assignedBy])

    def __repr__(self):
        return f"<RankHistory(user={self.userID}, rank={self.newRank}, date={self.createdAt})>"