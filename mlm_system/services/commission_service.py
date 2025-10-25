# bot/mlm_system/services/commission_service.py
"""
Commission calculation service - replaces old bonus_processor.
"""
from decimal import Decimal
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
import logging

from models import User, Purchase, Bonus
from mlm_system.config.ranks import RANK_CONFIG, Rank, PIONEER_BONUS_PERCENTAGE, REFERRAL_BONUS_PERCENTAGE, REFERRAL_BONUS_MIN_AMOUNT

logger = logging.getLogger(__name__)


class CommissionService:
    """Service for calculating MLM commissions."""

    def __init__(self, session: Session):
        self.session = session

    async def processPurchase(self, purchaseId: int) -> Dict:
        """
        Process all commissions for a purchase.
        Main entry point replacing process_purchase_bonuses.
        """
        purchase = self.session.query(Purchase).filter_by(
            purchaseID=purchaseId
        ).first()

        if not purchase:
            logger.error(f"Purchase {purchaseId} not found")
            return {"success": False, "error": "Purchase not found"}

        results = {
            "success": True,
            "purchase": purchaseId,
            "commissions": [],
            "totalDistributed": Decimal("0")
        }

        # 1. Calculate differential commissions
        differentialCommissions = await self._calculateDifferentialCommissions(purchase)

        # 2. Apply compression if needed
        compressedCommissions = await self._applyCompression(
            differentialCommissions,
            purchase
        )

        # 3. Apply Pioneer Bonus if applicable
        pioneeredCommissions = await self._applyPioneerBonus(
            compressedCommissions,
            purchase
        )

        # 4. Save all commissions to database
        for commission in pioneeredCommissions:
            await self._saveCommission(commission, purchase)
            results["totalDistributed"] += commission["amount"]
            results["commissions"].append(commission)

        # 5. Process referral bonus if applicable
        referralBonus = await self.processReferralBonus(purchase)
        if referralBonus:
            results["commissions"].append(referralBonus)
            results["totalDistributed"] += referralBonus["amount"]

        logger.info(
            f"Processed purchase {purchaseId}: "
            f"{len(results['commissions'])} commissions, "
            f"total {results['totalDistributed']}"
        )

        return results

    async def _calculateDifferentialCommissions(
            self,
            purchase: Purchase
    ) -> List[Dict]:
        """Calculate differential commissions up the chain."""
        commissions = []
        currentUser = purchase.user
        lastPercentage = Decimal("0")
        level = 1

        # Walk up the upline chain
        while currentUser.upline:
            uplineUser = self.session.query(User).filter_by(
                telegramID=currentUser.upline
            ).first()

            if not uplineUser:
                break

            # Check if upline is active
            if not uplineUser.isActive:
                # Mark for compression - will be handled in next step
                commissions.append({
                    "userId": uplineUser.userID,
                    "percentage": self._getUserRankPercentage(uplineUser),
                    "amount": Decimal("0"),
                    "level": level,
                    "rank": uplineUser.rank,
                    "isActive": False,
                    "compressed": True
                })
            else:
                # Calculate differential
                userPercentage = self._getUserRankPercentage(uplineUser)
                differential = userPercentage - lastPercentage

                if differential > 0:
                    amount = Decimal(str(purchase.packPrice)) * differential

                    commissions.append({
                        "userId": uplineUser.userID,
                        "percentage": differential,
                        "amount": amount,
                        "level": level,
                        "rank": uplineUser.rank,
                        "isActive": True,
                        "compressed": False
                    })

                    lastPercentage = userPercentage

            currentUser = uplineUser
            level += 1

            # Stop at max percentage
            if lastPercentage >= Decimal("0.18"):
                break

        return commissions

    async def _applyCompression(
            self,
            commissions: List[Dict],
            purchase: Purchase
    ) -> List[Dict]:
        """Apply compression - skip inactive users."""
        compressedCommissions = []
        pendingCompression = Decimal("0")

        for commission in commissions:
            if not commission["isActive"]:
                # Accumulate percentage for compression
                pendingCompression += commission["percentage"]
                logger.info(
                    f"Compressing inactive user {commission['userId']}, "
                    f"accumulating {commission['percentage']}%"
                )
            else:
                # Active user - gets their percentage + compressed
                totalPercentage = commission["percentage"] + pendingCompression
                commission["amount"] = Decimal(str(purchase.packPrice)) * totalPercentage
                commission["compressed"] = pendingCompression > 0
                commission["compressionAmount"] = pendingCompression

                compressedCommissions.append(commission)
                pendingCompression = Decimal("0")

        return compressedCommissions

    def _getUserRankPercentage(self, user: User) -> Decimal:
        """Get commission percentage for user's rank."""
        try:
            rank = Rank(user.rank)
            return RANK_CONFIG[rank]["percentage"]
        except (ValueError, KeyError):
            return RANK_CONFIG[Rank.START]["percentage"]

    async def _applyPioneerBonus(
            self,
            commissions: List[Dict],
            purchase: Purchase
    ) -> List[Dict]:
        """Apply Pioneer Bonus (+4%) for qualified users."""
        pioneeredCommissions = []

        for commission in commissions:
            user = self.session.query(User).filter_by(
                userID=commission["userId"]
            ).first()

            if user and user.mlmStatus:
                hasPioneerBonus = user.mlmStatus.get("hasPioneerBonus", False)

                if hasPioneerBonus:
                    # Add 4% bonus
                    pioneerAmount = Decimal(str(purchase.packPrice)) * PIONEER_BONUS_PERCENTAGE
                    commission["pioneerBonus"] = pioneerAmount
                    commission["amount"] += pioneerAmount

                    logger.info(
                        f"Pioneer bonus {pioneerAmount} added for user {user.userID}"
                    )

            pioneeredCommissions.append(commission)

        return pioneeredCommissions

    async def _saveCommission(self, commissionData: Dict, purchase: Purchase):
        """Save commission to database."""
        # Get user for owner fields
        user = self.session.query(User).filter_by(userID=commissionData["userId"]).first()
        if not user:
            logger.error(f"User {commissionData['userId']} not found for commission")
            return

        # Create bonus record - using actual field names from Bonus model
        bonus = Bonus()

        # Required fields
        bonus.userID = commissionData["userId"]
        bonus.downlineID = purchase.userID
        bonus.purchaseID = purchase.purchaseID

        # Denormalized data
        bonus.projectID = purchase.projectID
        bonus.optionID = purchase.optionID
        bonus.packQty = purchase.packQty
        bonus.packPrice = purchase.packPrice

        # MLM specific fields
        bonus.commissionType = "differential"
        bonus.uplineLevel = commissionData["level"]
        bonus.fromRank = commissionData["rank"]
        bonus.sourceRank = None  # Will be set for global pool
        bonus.bonusRate = float(commissionData["percentage"])
        bonus.bonusAmount = commissionData["amount"]
        bonus.compressionApplied = 1 if commissionData.get("compressed", False) else 0

        # Status
        bonus.status = "paid"
        bonus.notes = f"Level {commissionData['level']} commission"

        # AuditMixin fields - these are set automatically or manually
        bonus.ownerTelegramID = user.telegramID
        bonus.ownerEmail = user.email
        # createdAt and updatedAt are handled by AuditMixin defaults

        self.session.add(bonus)

        # Update user's passive balance
        user.balancePassive = (user.balancePassive or Decimal("0")) + commissionData["amount"]
        logger.info(
            f"Updated passive balance for user {user.userID}: "
            f"+{commissionData['amount']}"
        )

    async def processReferralBonus(self, purchase: Purchase) -> Optional[Dict]:
        """
        Process 1% referral bonus for direct sponsor.
        Only for purchases >= 5000.
        """
        if Decimal(str(purchase.packPrice)) < REFERRAL_BONUS_MIN_AMOUNT:
            return None

        # Get direct sponsor
        purchaseUser = purchase.user
        if not purchaseUser.upline:
            return None

        sponsor = self.session.query(User).filter_by(
            telegramID=purchaseUser.upline
        ).first()

        if not sponsor or not sponsor.isActive:
            return None

        # Calculate 1% bonus
        bonusAmount = Decimal(str(purchase.packPrice)) * REFERRAL_BONUS_PERCENTAGE

        # Save bonus
        bonus = Bonus()

        # Set fields properly
        bonus.userID = sponsor.userID
        bonus.downlineID = purchase.userID
        bonus.purchaseID = purchase.purchaseID
        bonus.projectID = purchase.projectID
        bonus.optionID = purchase.optionID
        bonus.packQty = purchase.packQty
        bonus.packPrice = purchase.packPrice

        bonus.commissionType = "referral"
        bonus.uplineLevel = 1
        bonus.fromRank = sponsor.rank
        bonus.bonusRate = float(REFERRAL_BONUS_PERCENTAGE)
        bonus.bonusAmount = bonusAmount
        bonus.compressionApplied = 0

        bonus.status = "paid"
        bonus.notes = "Referral bonus for direct sponsor"

        # Owner fields
        bonus.ownerTelegramID = sponsor.telegramID
        bonus.ownerEmail = sponsor.email

        self.session.add(bonus)

        # Update balance
        sponsor.balancePassive = (sponsor.balancePassive or Decimal("0")) + bonusAmount

        logger.info(f"Referral bonus {bonusAmount} for sponsor {sponsor.userID}")

        return {
            "userId": sponsor.userID,
            "amount": bonusAmount,
            "type": "referral"
        }