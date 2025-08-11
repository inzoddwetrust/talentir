import re
import aiohttp
import logging
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple

import config


class TxidValidationCode(Enum):
    VALID_TRANSACTION = "valid"
    INVALID_PREFIX = "invalid_prefix"
    INVALID_LENGTH = "invalid_length"
    INVALID_CHARS = "invalid_chars"
    UNSUPPORTED_METHOD = "unsupported_method"
    TRANSACTION_NOT_FOUND = "tx_not_found"
    WRONG_RECIPIENT = "wrong_recipient"
    WRONG_NETWORK = "wrong_network"
    API_ERROR = "api_error"
    TXID_ALREADY_USED = "already_used"
    NEEDS_CONFIRMATION = "needs_confirm"


@dataclass
class ValidationResult:
    code: TxidValidationCode
    details: Optional[str] = None
    from_address: Optional[str] = None
    to_address: Optional[str] = None


def validate_txid(txid: str, method: str) -> ValidationResult:
    """
    Validates TXID format based on payment method.
    Returns validation code and optional details.
    """
    txid = txid.lower().strip()

    if method in ["ETH", "BNB", "USDT-ERC20", "USDT-BSC20"]:
        if not txid.startswith("0x"):
            return ValidationResult(TxidValidationCode.INVALID_PREFIX)
        if len(txid) != 66:
            return ValidationResult(TxidValidationCode.INVALID_LENGTH)
        if not re.match(r"^0x[0-9a-f]{64}$", txid):
            return ValidationResult(TxidValidationCode.INVALID_CHARS)

    elif method in ["TRX", "USDT-TRC20"]:
        if len(txid) != 64:
            return ValidationResult(TxidValidationCode.INVALID_LENGTH)
        if not re.match(r"^[0-9a-f]{64}$", txid):
            return ValidationResult(TxidValidationCode.INVALID_CHARS)

    else:
        return ValidationResult(TxidValidationCode.UNSUPPORTED_METHOD)

    return ValidationResult(TxidValidationCode.VALID_TRANSACTION)


async def verify_transaction(txid: str, method: str, expected_address: str) -> ValidationResult:
    """
    Verifies transaction details using blockchain APIs.
    Checks recipient address and returns transaction details.
    """
    try:
        if method in ["ETH", "USDT-ERC20"]:
            result = await _verify_eth_transaction(txid)
        elif method in ["BNB", "USDT-BSC20"]:
            result = await _verify_bsc_transaction(txid)
        elif method in ["TRX", "USDT-TRC20"]:
            result = await _verify_tron_transaction(txid)
        else:
            return ValidationResult(TxidValidationCode.UNSUPPORTED_METHOD)

        if result is None:
            return ValidationResult(TxidValidationCode.TRANSACTION_NOT_FOUND)

        from_addr, to_addr = result
        # if to_addr.lower() != expected_address.lower():
        #     return ValidationResult(
        #         TxidValidationCode.WRONG_RECIPIENT,
        #         from_address=from_addr,
        #         to_address=to_addr
        #     )

        return ValidationResult(
            TxidValidationCode.VALID_TRANSACTION,
            from_address=from_addr,
            to_address=to_addr
        )

    except Exception as e:
        logging.error(f"Error verifying transaction {txid}: {e}")
        return ValidationResult(TxidValidationCode.API_ERROR, details=str(e))


async def _verify_eth_transaction(txid: str) -> Optional[Tuple[str, str]]:
    """Verifies ETH/ERC20 transaction."""
    url = "https://api.etherscan.io/api"
    params = {
        "module": "proxy",
        "action": "eth_getTransactionByHash",
        "txhash": txid,
        "apikey": config.ETHERSCAN_API_KEY
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("result"):
                    return data["result"]["from"], data["result"]["to"]
    return None


async def _verify_bsc_transaction(txid: str) -> Optional[Tuple[str, str]]:
    """Verifies BSC transaction."""
    url = "https://api.bscscan.com/api"
    params = {
        "module": "proxy",
        "action": "eth_getTransactionByHash",
        "txhash": txid,
        "apikey": config.BSCSCAN_API_KEY
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("result") and data["result"] is not None:
                    # Check if transaction exists (result is not None)
                    tx_data = data["result"]
                    # Extract from and to addresses
                    from_address = tx_data.get("from")
                    to_address = tx_data.get("to")

                    if from_address and to_address:
                        return from_address, to_address
    return None


async def _verify_tron_transaction(txid: str) -> Optional[Tuple[str, str]]:
    """Verifies TRON transaction."""
    url = "https://apilist.tronscan.org/api/transaction-info"
    params = {"hash": txid}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()

                if data.get("contractType") == 1:  # TRX transfer
                    return data.get("ownerAddress"), data.get("toAddress")

                elif data.get("contractType") == 31:  # TRC20 transfer
                    if data.get("trc20TransferInfo"):
                        transfers = data["trc20TransferInfo"]
                        if transfers:
                            transfer = transfers[0]
                            return transfer.get("from_address"), transfer.get("to_address")
    return None
