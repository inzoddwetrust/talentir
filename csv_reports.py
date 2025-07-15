import io
import csv
import logging
from typing import List, Dict, Any, Optional, Tuple, Callable
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import User, Purchase, Bonus, ActiveBalance, PassiveBalance

logger = logging.getLogger(__name__)

# Dictionary mapping report types to information about the report
REPORTS = {
    "team_full": {
        "name": "Team Full Report",
        "generator": lambda s, u, p: team_full_report(s, u, p)
    },
    "active_balance_history": {
        "name": "Active Balance History",
        "generator": lambda s, u, p: active_balance_history_report(s, u, p)
    },
    "passive_balance_history": {
        "name": "Passive Balance History",
        "generator": lambda s, u, p: passive_balance_history_report(s, u, p)
    }
}

# For backward compatibility and simpler usage
REPORT_TYPES = {key: info["name"] for key, info in REPORTS.items()}


def generate_csv_report(
        session: Session,
        user: User,
        report_type: str,
        params: Dict[str, Any] = None
) -> Optional[io.BytesIO]:
    """
    Generates a CSV report based on report type and parameters

    Args:
        session: Database session
        user: User object requesting the report
        report_type: Type of report (one of REPORTS keys)
        params: Additional parameters for report customization

    Returns:
        BytesIO object containing CSV data or None if report generation failed
    """
    if report_type not in REPORTS:
        logger.error(f"Unknown report type: {report_type}")
        return None

    try:
        if params is None:
            params = {}

        # Get report generator function
        report_info = REPORTS.get(report_type)
        report_generator = report_info["generator"]

        if not report_generator:
            logger.error(f"No generator implemented for report type: {report_type}")
            return None

        # Generate report data
        headers, data = report_generator(session, user, params)

        # Create CSV in memory - use StringIO first, then convert to BytesIO
        string_output = io.StringIO()
        writer = csv.writer(string_output, delimiter=';')  # Use semicolon for better Excel compatibility

        # Write headers
        writer.writerow(headers)

        # Write data rows
        for row in data:
            writer.writerow(row)

        # Convert to BytesIO
        output = io.BytesIO(string_output.getvalue().encode('utf-8-sig'))  # Use BOM for Excel compatibility

        # Return to the beginning of the buffer
        output.seek(0)
        return output

    except Exception as e:
        logger.error(f"Error generating {report_type} report: {e}", exc_info=True)
        return None


def team_full_report(session: Session, user: User, params: Dict[str, Any]) -> Tuple[List[str], List[List[Any]]]:
    """
    Generate full team report with all referrals and their statistics

    This report contains information about all users in the referral tree:
    - User ID
    - Full name
    - Registration date
    - Referral level (depth in the tree)
    - Number of direct referrals (users directly invited)
    - Total team size (all users in the subtree)
    - Total purchases amount made by the user
    - Bonus gained from this referral's purchases
    - List of referral's purchases (with blank cells for user data)

    Args:
        session: Database session
        user: User requesting the report
        params: Additional parameters

    Returns:
        Tuple of (headers, data_rows)
    """
    # Main headers for user information
    main_headers = ["ID", "Name", "Registration Date", "Level", "Direct Referrals", "Total Team", "Purchases Amount",
                    "Bonus Gained"]

    # Headers for purchase details (will leave blank cells for user data)
    purchase_headers = ["", "", "", "", "", "", "", "", "Purchase ID", "Purchase Date", "Project", "Shares", "Price"]

    # Combine all headers
    headers = main_headers + purchase_headers[8:]

    # Helper function to get team size recursively
    def get_team_size(telegram_id, visited=None):
        if visited is None:
            visited = set()

        referrals = session.query(User.telegramID).filter(
            User.upline == telegram_id
        ).all()

        total = 0
        for (ref_id,) in referrals:
            if ref_id not in visited:
                visited.add(ref_id)
                total += 1 + get_team_size(ref_id, visited)
        return total

    # Helper function to get referral tree recursively
    def get_referral_tree(telegram_id, level=1, visited=None):
        if visited is None:
            visited = set()

        if telegram_id in visited:
            return []

        visited.add(telegram_id)
        referrals = session.query(User).filter(User.upline == telegram_id).all()

        result = []
        for ref in referrals:
            # Get basic user info
            direct_refs_count = session.query(func.count(User.userID)).filter(
                User.upline == ref.telegramID
            ).scalar() or 0

            total_team = get_team_size(ref.telegramID)

            # Get total purchases amount
            purchases_sum = session.query(func.sum(Purchase.packPrice)).filter(
                Purchase.userID == ref.userID
            ).scalar() or 0

            # Calculate bonuses gained from this referral
            bonuses_gained = session.query(func.sum(Bonus.bonusAmount)).filter(
                Bonus.userID == user.userID,
                Bonus.downlineID == ref.userID
            ).scalar() or 0

            # Basic user info row
            user_row = [
                ref.userID,
                f"{ref.firstname} {ref.surname or ''}".strip(),
                ref.createdAt.strftime("%Y-%m-%d") if ref.createdAt else "",
                level,
                direct_refs_count,
                total_team,
                float(purchases_sum) if purchases_sum else 0,
                float(bonuses_gained) if bonuses_gained else 0
            ]

            # Add user row
            result.append(user_row)

            # Get detailed purchase information
            purchases = session.query(Purchase).filter(
                Purchase.userID == ref.userID
            ).order_by(Purchase.createdAt.desc()).all()

            # Add purchase rows with blank cells for user info
            for purchase in purchases:
                purchase_row = [""] * 8  # Empty cells for user data
                purchase_row.extend([
                    purchase.purchaseID,
                    purchase.createdAt.strftime("%Y-%m-%d %H:%M:%S") if purchase.createdAt else "",
                    purchase.projectName,
                    purchase.packQty,
                    purchase.packPrice
                ])
                result.append(purchase_row)

            # Add nested referrals
            result.extend(get_referral_tree(ref.telegramID, level + 1, visited))

        return result

    # Generate report data
    data = get_referral_tree(user.telegramID)

    return headers, data


def active_balance_history_report(session: Session, user: User, params: Dict[str, Any]) -> Tuple[List[str], List[List[Any]]]:
    """
    Generate a report of the user's active balance history

    This report contains information about all active balance transactions:
    - Transaction ID
    - Date and time
    - Amount
    - Status
    - Reason
    - Notes

    Args:
        session: Database session
        user: User requesting the report
        params: Additional parameters (not used currently)

    Returns:
        Tuple of (headers, data_rows)
    """
    # Define report headers
    headers = ["Transaction ID", "Date", "Amount", "Status", "Reason", "Details", "Notes"]

    # Get all active balance records for the user, ordered by date (newest first)
    records = session.query(ActiveBalance).filter(
        ActiveBalance.userID == user.userID
    ).order_by(ActiveBalance.createdAt.desc()).all()

    # Format data rows
    data = []
    for record in records:
        # Extract document ID from reason field if available
        doc_id = ""
        reason_type = ""
        if record.reason and '=' in record.reason:
            parts = record.reason.split('=')
            reason_type = parts[0]
            doc_id = parts[1]

        # Create a row for each record
        row = [
            record.paymentID,
            record.createdAt.strftime("%Y-%m-%d %H:%M:%S") if record.createdAt else "",
            record.amount,
            record.status,
            reason_type,
            doc_id,
            record.notes or ""
        ]
        data.append(row)

    return headers, data


def passive_balance_history_report(session: Session, user: User, params: Dict[str, Any]) -> Tuple[List[str], List[List[Any]]]:
    """
    Generate a report of the user's passive balance history

    This report contains information about all passive balance transactions:
    - Transaction ID
    - Date and time
    - Amount
    - Status
    - Reason
    - Notes

    Args:
        session: Database session
        user: User requesting the report
        params: Additional parameters (not used currently)

    Returns:
        Tuple of (headers, data_rows)
    """
    # Define report headers
    headers = ["Transaction ID", "Date", "Amount", "Status", "Reason", "Details", "Notes"]

    # Get all passive balance records for the user, ordered by date (newest first)
    records = session.query(PassiveBalance).filter(
        PassiveBalance.userID == user.userID
    ).order_by(PassiveBalance.createdAt.desc()).all()

    # Format data rows
    data = []
    for record in records:
        # Extract document ID from reason field if available
        doc_id = ""
        reason_type = ""
        if record.reason and '=' in record.reason:
            parts = record.reason.split('=')
            reason_type = parts[0]
            doc_id = parts[1]

        # Create a row for each record
        row = [
            record.paymentID,
            record.createdAt.strftime("%Y-%m-%d %H:%M:%S") if record.createdAt else "",
            record.amount,
            record.status,
            reason_type,
            doc_id,
            record.notes or ""
        ]
        data.append(row)

    return headers, data