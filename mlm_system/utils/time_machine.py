# bot/mlm_system/utils/time_machine.py
"""
Time machine for testing - controls virtual time in the system.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class TimeMachine:
    """Singleton for managing system time."""

    _instance = None
    _virtualTime: Optional[datetime] = None
    _isTestMode: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def now(self) -> datetime:
        """Get current system time (real or virtual)."""
        if self._isTestMode and self._virtualTime:
            return self._virtualTime
        return datetime.now(timezone.utc)

    @property
    def currentMonth(self) -> str:
        """Get current month in YYYY-MM format."""
        return self.now.strftime('%Y-%m')

    @property
    def isGraceDay(self) -> bool:
        """Check if today is Grace Day (1st of month)."""
        return self.now.day == 1

    @property
    def isMonthEnd(self) -> bool:
        """Check if today is last day of month."""
        tomorrow = self.now + timedelta(days=1)
        return tomorrow.month != self.now.month

    def setTime(self, newTime: datetime, adminId: Optional[int] = None):
        """Set virtual time for testing."""
        self._isTestMode = True
        self._virtualTime = newTime
        logger.info(f"Virtual time set to {newTime} by admin {adminId}")

    def advanceTime(self, days: int = 0, hours: int = 0):
        """Advance virtual time forward."""
        if not self._isTestMode:
            raise ValueError("Cannot advance time when not in test mode")

        self._virtualTime += timedelta(days=days, hours=hours)
        logger.info(f"Time advanced to {self._virtualTime}")

    def resetToRealTime(self):
        """Return to real time."""
        self._isTestMode = False
        self._virtualTime = None
        logger.info("Returned to real time")


# Global instance
timeMachine = TimeMachine()