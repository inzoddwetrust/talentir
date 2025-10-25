# bot/mlm_system/services/volume_service.py
"""
Volume tracking service for MLM system.
"""
from decimal import Decimal
from typing import List, Dict
from sqlalchemy.orm import Session
import logging

from models import User, Purchase
from mlm_system.utils.time_machine import timeMachine

logger = logging.getLogger(__name__)


class VolumeService:
    """Service for tracking personal and team volumes."""

    def __init__(self, session: Session):
        self.session = session

    async def updatePurchaseVolumes(self, purchase: Purchase):
        """Update volumes after a purchase."""
        purchaseAmount = Decimal(str(purchase.packPrice))
        currentMonth = timeMachine.currentMonth

        # Update purchaser's personal volume
        user = purchase.user
        await self._updatePersonalVolume(user, purchaseAmount, currentMonth)

        # Update team volumes up the chain
        await self._updateTeamVolumeChain(user, purchaseAmount)

    async def _updatePersonalVolume(
            self,
            user: User,
            amount: Decimal,
            currentMonth: str
    ):
        """Update user's personal volume."""
        # Update total PV
        user.personalVolumeTotal = (user.personalVolumeTotal or Decimal("0")) + amount

        # Update monthly PV in JSON
        if not user.mlmVolumes:
            user.mlmVolumes = {}

        user.mlmVolumes["personalTotal"] = str(user.personalVolumeTotal)
        user.mlmVolumes["monthlyPV"] = str(
            Decimal(user.mlmVolumes.get("monthlyPV", "0")) + amount
        )

        # Check activation status
        monthlyPv = Decimal(user.mlmVolumes["monthlyPV"])
        if monthlyPv >= Decimal("200"):
            user.isActive = True
            user.lastActiveMonth = currentMonth

            if user.mlmStatus:
                user.mlmStatus["lastActiveMonth"] = currentMonth

        logger.info(
            f"Updated PV for user {user.userID}: "
            f"total={user.personalVolumeTotal}, monthly={monthlyPv}"
        )

    async def _updateTeamVolumeChain(self, user: User, amount: Decimal):
        """Update team volumes up the upline chain."""
        currentUser = user

        while currentUser.upline:
            uplineUser = self.session.query(User).filter_by(
                telegramID=currentUser.upline
            ).first()

            if not uplineUser:
                break

            # Update team volume
            uplineUser.teamVolumeTotal = (
                                                 uplineUser.teamVolumeTotal or Decimal("0")
                                         ) + amount

            if not uplineUser.mlmVolumes:
                uplineUser.mlmVolumes = {}

            uplineUser.mlmVolumes["teamTotal"] = str(uplineUser.teamVolumeTotal)

            logger.info(
                f"Updated TV for user {uplineUser.userID}: "
                f"total={uplineUser.teamVolumeTotal}"
            )

            currentUser = uplineUser

    async def getBestBranches(
            self,
            userId: int,
            count: int = 2
    ) -> List[Dict]:
        """Get top N branches by volume for a user."""
        user = self.session.query(User).filter_by(userID=userId).first()
        if not user:
            return []

        # Get direct referrals
        directReferrals = self.session.query(User).filter_by(
            upline=user.telegramID
        ).all()

        branches = []
        for referral in directReferrals:
            # Calculate total volume in this branch
            branchVolume = await self._calculateBranchVolume(referral)

            branches.append({
                "rootUser": referral,
                "rootUserId": referral.userID,
                "volume": branchVolume,
                "hasDirector": await self._checkForDirectorInBranch(referral)
            })

        # Sort by volume and return top N
        branches.sort(key=lambda x: x["volume"], reverse=True)
        return branches[:count]

    async def _calculateBranchVolume(self, rootUser: User) -> Decimal:
        """Calculate total volume in a branch recursively."""
        totalVolume = rootUser.teamVolumeTotal or Decimal("0")

        # Add personal volume
        totalVolume += rootUser.personalVolumeTotal or Decimal("0")

        return totalVolume

    async def _checkForDirectorInBranch(self, rootUser: User) -> bool:
        """Check if there's a Director rank in the branch."""
        if rootUser.rank == "director":
            return True

        # Check all downline recursively
        downline = self.session.query(User).filter_by(
            upline=rootUser.telegramID
        ).all()

        for user in downline:
            if await self._checkForDirectorInBranch(user):
                return True

        return False

    async def resetMonthlyVolumes(self):
        """Reset all monthly volumes - called on 1st of month."""
        logger.info(f"Resetting monthly volumes for {timeMachine.currentMonth}")

        # Reset all users' monthly PV
        allUsers = self.session.query(User).all()

        for user in allUsers:
            if user.mlmVolumes:
                user.mlmVolumes["monthlyPV"] = "0"

            # Reset monthly activity
            user.isActive = False

        self.session.commit()
        logger.info(f"Reset monthly volumes for {len(allUsers)} users")