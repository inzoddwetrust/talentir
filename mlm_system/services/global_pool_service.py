# bot/mlm_system/services/global_pool_service.py
"""
Global Pool management service for MLM system.
"""
from decimal import Decimal
from typing import List, Dict
from sqlalchemy.orm import Session
import logging
import json

from models import User, GlobalPool, Bonus, MonthlyStats
from mlm_system.config.ranks import GLOBAL_POOL_PERCENTAGE
from mlm_system.utils.time_machine import timeMachine
from mlm_system.services.volume_service import VolumeService

logger = logging.getLogger(__name__)


class GlobalPoolService:
    """Service for managing Global Pool distributions."""

    def __init__(self, session: Session):
        self.session = session
        self.volumeService = VolumeService(session)

    async def calculateMonthlyPool(self) -> Dict:
        """
        Calculate Global Pool for current month.
        Called on 3rd of each month.
        """
        currentMonth = timeMachine.currentMonth

        # Check if already calculated for this month
        existing = self.session.query(GlobalPool).filter_by(
            month=currentMonth
        ).first()

        if existing:
            logger.warning(f"Global Pool already calculated for {currentMonth}")
            return {
                "success": False,
                "error": "Already calculated",
                "poolId": existing.poolID
            }

        # Calculate total company volume for the month
        totalVolume = await self._calculateCompanyMonthlyVolume()

        # Calculate pool size (2% of total volume)
        poolSize = totalVolume * GLOBAL_POOL_PERCENTAGE

        # Find qualified users
        qualifiedUsers = await self._findQualifiedUsers()
        qualifiedCount = len(qualifiedUsers)

        # Calculate per-user amount
        perUserAmount = Decimal("0")
        if qualifiedCount > 0:
            perUserAmount = poolSize / qualifiedCount

        # Create GlobalPool record
        pool = GlobalPool(
            month=currentMonth,
            totalCompanyVolume=totalVolume,
            poolPercentage=GLOBAL_POOL_PERCENTAGE,
            poolSize=poolSize,
            qualifiedUsersCount=qualifiedCount,
            perUserAmount=perUserAmount,
            status="calculated",
            qualifiedUsers=json.dumps([u["userId"] for u in qualifiedUsers])
        )

        self.session.add(pool)
        self.session.commit()

        logger.info(
            f"Global Pool calculated for {currentMonth}: "
            f"volume={totalVolume}, pool={poolSize}, "
            f"qualified={qualifiedCount}, per_user={perUserAmount}"
        )

        return {
            "success": True,
            "poolId": pool.poolID,
            "month": currentMonth,
            "totalVolume": totalVolume,
            "poolSize": poolSize,
            "qualifiedUsers": qualifiedCount,
            "perUserAmount": perUserAmount
        }

    async def _calculateCompanyMonthlyVolume(self) -> Decimal:
        """Calculate total company volume for current month."""
        currentMonth = timeMachine.currentMonth

        # Get all users' monthly PV
        totalVolume = Decimal("0")
        users = self.session.query(User).filter(
            User.isActive == True
        ).all()

        for user in users:
            if user.mlmVolumes:
                monthlyPV = Decimal(user.mlmVolumes.get("monthlyPV", "0"))
                totalVolume += monthlyPV

        return totalVolume

    async def _findQualifiedUsers(self) -> List[Dict]:
        """
        Find users qualified for Global Pool.
        Requirement: 2 Directors in different direct branches.
        """
        qualifiedUsers = []

        # Get all potential qualifiers (could be Directors themselves)
        allUsers = self.session.query(User).filter(
            User.isActive == True
        ).all()

        for user in allUsers:
            # Check if user has 2 directors in different branches
            if await self._checkGlobalPoolQualification(user):
                qualifiedUsers.append({
                    "userId": user.userID,
                    "telegramId": user.telegramID,
                    "rank": user.rank
                })

        return qualifiedUsers

    async def _checkGlobalPoolQualification(self, user: User) -> bool:
        """
        Check if user qualifies for Global Pool.
        Need 2 Directors in top 2 branches.
        """
        # Get top 2 branches
        branches = await self.volumeService.getBestBranches(user.userID, 2)

        if len(branches) < 2:
            return False

        # Check if both branches have Directors
        directorsCount = 0
        for branch in branches:
            if branch.get("hasDirector", False):
                directorsCount += 1

        return directorsCount >= 2

    async def distributeGlobalPool(self) -> Dict:
        """
        Distribute Global Pool to qualified users.
        Called on 5th of each month.
        """
        currentMonth = timeMachine.currentMonth

        # Get calculated pool for current month
        pool = self.session.query(GlobalPool).filter_by(
            month=currentMonth,
            status="calculated"
        ).first()

        if not pool:
            logger.error(f"No calculated pool found for {currentMonth}")
            return {
                "success": False,
                "error": "Pool not calculated"
            }

        if pool.qualifiedUsersCount == 0:
            pool.status = "distributed"
            pool.distributedAt = timeMachine.now
            logger.info(f"No qualified users for Global Pool {currentMonth}")
            return {
                "success": True,
                "distributed": 0,
                "total": Decimal("0")
            }

        # Parse qualified users
        qualifiedUserIds = json.loads(pool.qualifiedUsers or "[]")

        distributed = 0
        totalDistributed = Decimal("0")

        for userId in qualifiedUserIds:
            user = self.session.query(User).filter_by(userID=userId).first()
            if not user:
                logger.error(f"User {userId} not found for Global Pool distribution")
                continue

            # Create bonus record
            bonus = Bonus()
            bonus.userID = userId
            bonus.downlineID = None  # No specific downline for Global Pool
            bonus.purchaseID = None  # No specific purchase

            bonus.commissionType = "global_pool"
            bonus.fromRank = user.rank
            bonus.bonusRate = float(GLOBAL_POOL_PERCENTAGE)
            bonus.bonusAmount = pool.perUserAmount
            bonus.compressionApplied = 0

            bonus.status = "paid"
            bonus.notes = f"Global Pool for {currentMonth}"

            # Owner fields
            bonus.ownerTelegramID = user.telegramID
            bonus.ownerEmail = user.email

            self.session.add(bonus)

            # Update user's passive balance
            user.balancePassive = (user.balancePassive or Decimal("0")) + pool.perUserAmount

            # Update monthly stats if exists
            monthlyStats = self.session.query(MonthlyStats).filter_by(
                userID=userId,
                month=currentMonth
            ).first()

            if monthlyStats:
                monthlyStats.globalPoolEarned = pool.perUserAmount

            distributed += 1
            totalDistributed += pool.perUserAmount

            logger.info(
                f"Global Pool {pool.perUserAmount} distributed to user {userId}"
            )

        # Update pool status
        pool.status = "distributed"
        pool.distributedAt = timeMachine.now

        self.session.commit()

        logger.info(
            f"Global Pool distribution complete: "
            f"distributed={distributed}, total={totalDistributed}"
        )

        return {
            "success": True,
            "distributed": distributed,
            "total": totalDistributed,
            "perUser": pool.perUserAmount
        }

    async def getPoolHistory(self, months: int = 6) -> List[Dict]:
        """Get Global Pool history for last N months."""
        pools = self.session.query(GlobalPool).order_by(
            GlobalPool.createdAt.desc()
        ).limit(months).all()

        history = []
        for pool in pools:
            history.append({
                "month": pool.month,
                "totalVolume": float(pool.totalCompanyVolume),
                "poolSize": float(pool.poolSize),
                "qualified": pool.qualifiedUsersCount,
                "perUser": float(pool.perUserAmount or 0),
                "status": pool.status,
                "distributedAt": pool.distributedAt.isoformat() if pool.distributedAt else None
            })

        return history

    async def checkUserQualification(self, userId: int) -> Dict:
        """Check if specific user qualifies for Global Pool."""
        user = self.session.query(User).filter_by(userID=userId).first()

        if not user:
            return {
                "qualified": False,
                "reason": "User not found"
            }

        if not user.isActive:
            return {
                "qualified": False,
                "reason": "User not active"
            }

        # Get branches info
        branches = await self.volumeService.getBestBranches(userId, 2)

        directorsInBranches = 0
        branchesInfo = []

        for i, branch in enumerate(branches):
            hasDirector = branch.get("hasDirector", False)
            if hasDirector:
                directorsInBranches += 1

            branchesInfo.append({
                "branch": i + 1,
                "volume": float(branch["volume"]),
                "hasDirector": hasDirector,
                "rootUserId": branch["rootUserId"]
            })

        qualified = directorsInBranches >= 2

        return {
            "qualified": qualified,
            "reason": "Qualified" if qualified else f"Only {directorsInBranches} Director branches",
            "branches": branchesInfo,
            "directorsCount": directorsInBranches
        }