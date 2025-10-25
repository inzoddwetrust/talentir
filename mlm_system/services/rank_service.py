# bot/mlm_system/services/rank_service.py
"""
Rank management service for MLM system.
"""
from decimal import Decimal
from typing import Optional, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func
import logging

from models import User, Bonus, RankHistory, MonthlyStats
from mlm_system.config.ranks import RANK_CONFIG, Rank
from mlm_system.utils.time_machine import timeMachine

logger = logging.getLogger(__name__)


class RankService:
    """Service for managing user ranks and qualifications."""

    def __init__(self, session: Session):
        self.session = session

    async def checkRankQualification(self, userId: int) -> Optional[str]:
        """
        Check if user qualifies for a new rank.
        Returns new rank if qualified, None otherwise.
        """
        user = self.session.query(User).filter_by(userID=userId).first()
        if not user:
            return None

        currentRank = user.rank or "start"

        # Check each rank from highest to lowest
        for rankEnum in [Rank.DIRECTOR, Rank.LEADERSHIP, Rank.GROWTH, Rank.BUILDER]:
            rank = rankEnum.value

            # Skip if already at or above this rank
            if self._compareRanks(currentRank, rank) >= 0:
                continue

            # Check qualification
            if await self._isQualifiedForRank(user, rank):
                logger.info(f"User {userId} qualified for rank {rank}")
                return rank

        return None

    async def _isQualifiedForRank(self, user: User, rank: str) -> bool:
        """Check if user meets requirements for specific rank."""
        try:
            rankEnum = Rank(rank)
            requirements = RANK_CONFIG[rankEnum]
        except (ValueError, KeyError):
            return False

        # Check team volume
        teamVolume = user.teamVolumeTotal or Decimal("0")
        if teamVolume < requirements["teamVolumeRequired"]:
            return False

        # Check active partners
        activePartners = await self._countActivePartners(user)
        if activePartners < requirements["activePartnersRequired"]:
            return False

        return True

    async def _countActivePartners(self, user: User) -> int:
        """Count active partners in user's structure."""
        # Count only direct referrals who are active
        activeCount = self.session.query(func.count(User.userID)).filter(
            User.upline == user.telegramID,
            User.isActive == True
        ).scalar() or 0

        return activeCount

    async def updateUserRank(self, userId: int, newRank: str, method: str = "natural") -> bool:
        """
        Update user's rank and record in history.
        Method: 'natural', 'assigned', 'promotion'
        """
        user = self.session.query(User).filter_by(userID=userId).first()
        if not user:
            return False

        oldRank = user.rank

        # Update rank
        user.rank = newRank

        # Update MLM status
        if not user.mlmStatus:
            user.mlmStatus = {}
        user.mlmStatus["rankQualifiedAt"] = timeMachine.now.isoformat()

        # Create history record
        history = RankHistory(
            userID=userId,
            previousRank=oldRank,
            newRank=newRank,
            teamVolume=user.teamVolumeTotal,
            activePartners=await self._countActivePartners(user),
            qualificationMethod=method
        )
        self.session.add(history)

        logger.info(f"User {userId} rank updated: {oldRank} -> {newRank} ({method})")
        return True

    async def assignRankByFounder(
            self,
            userId: int,
            newRank: str,
            founderId: int
    ) -> bool:
        """Assign rank manually by founder."""
        user = self.session.query(User).filter_by(userID=userId).first()
        founder = self.session.query(User).filter_by(userID=founderId).first()

        if not user or not founder:
            return False

        # Check if assigner is founder
        if not founder.mlmStatus or not founder.mlmStatus.get("isFounder", False):
            logger.error(f"User {founderId} is not a founder")
            return False

        # Update rank
        user.rank = newRank
        user.assignedRank = newRank

        if not user.mlmStatus:
            user.mlmStatus = {}
        user.mlmStatus["assignedRank"] = newRank
        user.mlmStatus["rankQualifiedAt"] = timeMachine.now.isoformat()

        # Create history record
        history = RankHistory(
            userID=userId,
            previousRank=user.rank,
            newRank=newRank,
            teamVolume=user.teamVolumeTotal,
            activePartners=await self._countActivePartners(user),
            qualificationMethod="assigned",
            assignedBy=founderId
        )
        self.session.add(history)

        logger.info(f"Rank {newRank} assigned to user {userId} by founder {founderId}")
        return True

    async def getUserActiveRank(self, userId: int) -> str:
        """Get user's active rank (considering activity status)."""
        user = self.session.query(User).filter_by(userID=userId).first()
        if not user:
            return "start"

        # If user is not active, they can't use their rank
        if not user.isActive:
            return "start"

        # If rank was assigned, use it regardless of qualifications
        if user.assignedRank:
            return user.assignedRank

        return user.rank or "start"

    async def updateMonthlyActivity(self, userId: int) -> bool:
        """Update user's monthly activity status."""
        user = self.session.query(User).filter_by(userID=userId).first()
        if not user:
            return False

        # Check monthly PV
        monthlyPV = Decimal("0")
        if user.mlmVolumes:
            monthlyPV = Decimal(user.mlmVolumes.get("monthlyPV", "0"))

        # Update activity status
        isActive = monthlyPV >= Decimal("200")
        user.isActive = isActive

        if user.mlmStatus:
            user.mlmStatus["lastActiveMonth"] = timeMachine.currentMonth if isActive else None

        logger.info(f"User {userId} activity updated: {isActive} (PV: {monthlyPV})")
        return True

    async def checkAllRanks(self) -> Dict[str, int]:
        """Check and update ranks for all users."""
        results = {
            "checked": 0,
            "updated": 0,
            "errors": 0
        }

        users = self.session.query(User).all()

        for user in users:
            try:
                results["checked"] += 1

                # Check for new rank qualification
                newRank = await self.checkRankQualification(user.userID)
                if newRank:
                    success = await self.updateUserRank(
                        user.userID,
                        newRank,
                        "natural"
                    )
                    if success:
                        results["updated"] += 1
            except Exception as e:
                logger.error(f"Error checking rank for user {user.userID}: {e}")
                results["errors"] += 1

        self.session.commit()

        logger.info(
            f"Rank check complete: checked={results['checked']}, "
            f"updated={results['updated']}, errors={results['errors']}"
        )

        return results

    def _compareRanks(self, rank1: str, rank2: str) -> int:
        """
        Compare two ranks.
        Returns: -1 if rank1 < rank2, 0 if equal, 1 if rank1 > rank2
        """
        rankOrder = {
            "start": 0,
            "builder": 1,
            "growth": 2,
            "leadership": 3,
            "director": 4
        }

        value1 = rankOrder.get(rank1, 0)
        value2 = rankOrder.get(rank2, 0)

        if value1 < value2:
            return -1
        elif value1 > value2:
            return 1
        else:
            return 0

    async def saveMonthlyStats(self, userId: int) -> bool:
        """Save monthly statistics snapshot for user."""
        user = self.session.query(User).filter_by(userID=userId).first()
        if not user:
            return False

        currentMonth = timeMachine.currentMonth

        # Check if stats already exist for this month
        existing = self.session.query(MonthlyStats).filter_by(
            userID=userId,
            month=currentMonth
        ).first()

        if existing:
            logger.info(f"Monthly stats already exist for user {userId}, month {currentMonth}")
            return False

        # Calculate stats
        monthlyPV = Decimal("0")
        if user.mlmVolumes:
            monthlyPV = Decimal(user.mlmVolumes.get("monthlyPV", "0"))

        # Get commission sum for the month
        commissionsEarned = self.session.query(
            func.sum(Bonus.bonusAmount)
        ).filter(
            Bonus.userID == userId,
            func.strftime('%Y-%m', Bonus.createdAt) == currentMonth
        ).scalar() or Decimal("0")

        # Create stats record
        stats = MonthlyStats(
            userID=userId,
            month=currentMonth,
            personalVolume=monthlyPV,
            teamVolume=user.teamVolumeTotal or Decimal("0"),
            activePartnersCount=await self._countActivePartners(user),
            directReferralsCount=self.session.query(func.count(User.userID)).filter(
                User.upline == user.telegramID
            ).scalar() or 0,
            totalTeamSize=await self._countTotalTeamSize(user),
            activeRank=await self.getUserActiveRank(userId),
            commissionsEarned=commissionsEarned,
            bonusesEarned=Decimal("0"),  # Will be filled separately
            globalPoolEarned=Decimal("0"),  # Will be filled by GlobalPoolService
            wasActive=1 if user.isActive else 0
        )

        self.session.add(stats)

        logger.info(f"Monthly stats saved for user {userId}, month {currentMonth}")
        return True

    async def _countTotalTeamSize(self, user: User) -> int:
        """Count total team size recursively."""

        def countDownline(telegramId: int) -> int:
            directReferrals = self.session.query(User).filter(
                User.upline == telegramId
            ).all()

            count = len(directReferrals)
            for ref in directReferrals:
                count += countDownline(ref.telegramID)

            return count

        return countDownline(user.telegramID)