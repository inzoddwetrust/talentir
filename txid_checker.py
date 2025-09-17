import re
import aiohttp
import logging
import json
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
    Uses Etherscan API V2 for all EVM chains.
    """
    try:
        logging.info(f"Starting verification for txid: {txid}, method: {method}")

        if method in ["ETH", "USDT-ERC20"]:
            result = await _verify_evm_transaction_v2(txid, chain_id=1)  # Ethereum mainnet
        elif method in ["BNB", "USDT-BSC20"]:
            result = await _verify_evm_transaction_v2(txid, chain_id=56)  # BSC mainnet
        elif method in ["TRX", "USDT-TRC20"]:
            result = await _verify_tron_transaction(txid)  # TRON не EVM, оставляем старый API
        else:
            return ValidationResult(TxidValidationCode.UNSUPPORTED_METHOD)

        logging.info(f"Verification result: {result}")

        if result is None:
            return ValidationResult(TxidValidationCode.TRANSACTION_NOT_FOUND)

        from_addr, to_addr = result

        return ValidationResult(
            TxidValidationCode.VALID_TRANSACTION,
            from_address=from_addr,
            to_address=to_addr
        )

    except Exception as e:
        logging.error(f"Error verifying transaction {txid}: {e}", exc_info=True)
        return ValidationResult(TxidValidationCode.API_ERROR, details=str(e))


async def _verify_evm_transaction_v2(txid: str, chain_id: int) -> Optional[Tuple[str, str]]:
    """
    Universal EVM transaction verifier using Etherscan API V2.
    Works for ETH, BSC, and other EVM chains.

    Chain IDs:
    - Ethereum: 1
    - BSC: 56
    """
    url = "https://api.etherscan.io/v2/api"

    # Using JSON-RPC method through V2 API
    params = {
        "chainid": chain_id,
        "module": "proxy",
        "action": "eth_getTransactionByHash",
        "txhash": txid,
        "apikey": config.ETHERSCAN_API_KEY  # V2 uses single API key for all chains
    }

    logging.info(f"V2 API request - Chain: {chain_id}, TXID: {txid}")

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            logging.info(f"V2 API response status: {response.status}")

            if response.status == 200:
                try:
                    data = await response.json()
                    logging.info(f"V2 API response structure: {data.keys() if isinstance(data, dict) else type(data)}")

                    # Check for API errors
                    if data.get("status") == "0":
                        error_message = data.get("message", "Unknown error")
                        logging.error(f"V2 API error: {error_message}")

                        # Check for specific error messages
                        if "Invalid API Key" in error_message:
                            logging.error("API Key issue - check ETHERSCAN_API_KEY in .env")
                        elif "chain not supported" in error_message:
                            logging.error(f"Chain {chain_id} not supported by this API key")

                        return None

                    # Get transaction data from result
                    result = data.get("result")

                    if result and isinstance(result, dict):
                        from_address = result.get("from")
                        to_address = result.get("to")

                        logging.info(f"V2 transaction found - from: {from_address}, to: {to_address}")

                        if from_address and to_address:
                            return from_address, to_address
                    elif result is None or result == "null":
                        logging.info(f"Transaction not found: {txid}")
                        return None
                    else:
                        logging.warning(f"Unexpected result format: {type(result)}, value: {result}")

                except Exception as e:
                    logging.error(f"Error parsing V2 API response: {e}", exc_info=True)
            else:
                text = await response.text()
                logging.error(f"Non-200 response from V2 API: {response.status}, body: {text[:500]}")

    return None


async def _verify_tron_transaction(txid: str) -> Optional[Tuple[str, str]]:
    """
    Verifies TRON transaction.
    TRON is not EVM, so we keep using their native API.
    """
    url = "https://apilist.tronscan.org/api/transaction-info"
    params = {"hash": txid}

    logging.info(f"TRON API request for txid: {txid}")

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                try:
                    # First try to get response as text to log it
                    text = await response.text()
                    logging.info(f"TRON API raw response: {text[:500]}")

                    # Parse JSON
                    data = json.loads(text)

                    if isinstance(data, dict):
                        # TRX transfer
                        if data.get("contractType") == 1:
                            return data.get("ownerAddress"), data.get("toAddress")

                        # TRC20 transfer
                        elif data.get("contractType") == 31:
                            if data.get("trc20TransferInfo"):
                                transfers = data["trc20TransferInfo"]
                                if transfers and isinstance(transfers, list):
                                    transfer = transfers[0]
                                    return transfer.get("from_address"), transfer.get("to_address")
                    else:
                        logging.error(f"TRON API returned non-dict: {type(data)}")

                except Exception as e:
                    logging.error(f"Error parsing TRON response: {e}", exc_info=True)

    return None