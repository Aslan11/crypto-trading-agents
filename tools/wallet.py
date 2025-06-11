"""Wallet signing and sending utilities for EVM chains."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Dict

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.common import RetryPolicy


class SignedTx(BaseModel):
    rawTransaction: str
    hash: str


@activity.defn
async def build_signed_tx(raw_tx: dict, private_key: str) -> dict:
    """Sign ``raw_tx`` with ``private_key`` and return hex data."""
    from eth_account import Account

    signed = Account.sign_transaction(raw_tx, private_key)
    return {
        "rawTransaction": signed.rawTransaction.hex(),
        "hash": signed.hash.hex(),
    }


@activity.defn
async def send_tx(signed_hex: str, rpc_url: str) -> str:
    """Broadcast a signed transaction and wait for confirmation."""
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    tx_hash = w3.eth.send_raw_transaction(
        bytes.fromhex(signed_hex[2:]) if signed_hex.startswith("0x") else bytes.fromhex(signed_hex)
    )
    w3.eth.wait_for_transaction_receipt(tx_hash)
    return tx_hash.hex()


@workflow.defn
class SignAndSendTx:
    """Workflow to sign a transaction and send it to an EVM chain."""

    @workflow.run
    async def run(self, raw_tx: dict, wallet_label: str, rpc_url: str) -> Dict[str, str]:
        key_env = f"WALLET_{wallet_label.upper()}_KEY"
        privkey = os.getenv(key_env)
        if not privkey:
            raise RuntimeError(f"Missing environment variable: {key_env}")

        signed = await workflow.execute_activity(
            build_signed_tx,
            raw_tx,
            privkey,
            schedule_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        tx_hash = await workflow.execute_activity(
            send_tx,
            signed["rawTransaction"],
            rpc_url,
            schedule_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return {"tx_hash": tx_hash}

