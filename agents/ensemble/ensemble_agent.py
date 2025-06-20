"""Aggregate strategy signals and broadcast trade intents to other agents."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any, Dict

import aiohttp

import secrets

try:
    import openai
except Exception:  # pragma: no cover - optional dependency
    openai = None

from agents.workflows import ExecutionLedgerWorkflow

from agents.shared_bus import enqueue_intent
from agents.workflows import EnsembleWorkflow
from tools.intent_bus import IntentBus
from tools.risk import PreTradeRiskCheck
from agents.utils import print_banner, format_log
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
INTENT_QTY = float(os.environ.get("INTENT_QTY", "1"))
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
LEDGER_WF_ID = "mock-ledger"

logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

STOP_EVENT = asyncio.Event()
ENSEMBLE_WF_ID = "ensemble-agent"
INTENT_BUS_WF_ID = "intent-bus"
TEMPORAL_CLIENT: Client | None = None


async def _get_client() -> Client:
    global TEMPORAL_CLIENT
    if TEMPORAL_CLIENT is None:
        address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
        TEMPORAL_CLIENT = await Client.connect(address, namespace=namespace)
    return TEMPORAL_CLIENT


async def _ensure_workflow(client: Client) -> None:
    handle = client.get_workflow_handle(ENSEMBLE_WF_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                EnsembleWorkflow.run,
                id=ENSEMBLE_WF_ID,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
            )
        else:
            raise


async def _ensure_intent_bus(client: Client) -> None:
    """Ensure the IntentBus workflow is running."""
    handle = client.get_workflow_handle(INTENT_BUS_WF_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                IntentBus.run,
                id=INTENT_BUS_WF_ID,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
            )
        else:
            raise


async def _ensure_ledger(client: Client) -> None:
    """Ensure the execution ledger workflow is running."""
    handle = client.get_workflow_handle(LEDGER_WF_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                ExecutionLedgerWorkflow.run,
                id=LEDGER_WF_ID,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
            )
        else:
            raise


async def _get_ledger_status(client: Client) -> Dict[str, Any]:
    await _ensure_ledger(client)
    handle = client.get_workflow_handle(LEDGER_WF_ID)
    cash = await handle.query("get_cash")
    try:
        positions = await handle.query("get_positions")
        entry_prices = await handle.query("get_entry_prices")
    except Exception:  # pragma: no cover - older workflow version
        positions = {}
        entry_prices = {}
    return {"cash": cash, "positions": positions, "entry_prices": entry_prices}


async def _fetch(
    session: aiohttp.ClientSession, url: str, params: Dict[str, Any] | None = None
) -> Any | None:
    try:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.warning("GET %s returned %s", url, resp.status)
    except Exception as exc:
        logger.error("GET %s failed: %s", url, exc)
    return None


async def _post(session: aiohttp.ClientSession, url: str, json: Dict[str, Any]) -> Any | None:
    try:
        async with session.post(url, json=json) as resp:
            if resp.status in (200, 202, 204):
                if resp.content_type == "application/json":
                    return await resp.json()
                return None
            logger.warning("POST %s returned %s", url, resp.status)
    except Exception as exc:
        logger.error("POST %s failed: %s", url, exc)
    return None


async def _poll_vectors(session: aiohttp.ClientSession) -> None:
    cursor = 0
    while not STOP_EVENT.is_set():
        url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
        data = await _fetch(session, url, params={"after": cursor})
        if not data:
            await asyncio.sleep(1)
            continue
        for evt in data:
            symbol = evt.get("symbol")
            ts = evt.get("ts")
            vec = evt.get("data", {})
            price = vec.get("mid")
            if symbol and ts and price is not None:
                try:
                    client = await _get_client()
                    await _ensure_workflow(client)
                    handle = client.get_workflow_handle(ENSEMBLE_WF_ID)
                    await handle.signal("update_price", args=[symbol, float(price)])
                    cursor = max(cursor, ts)
                except RPCError as err:  # pragma: no cover - network failures
                    logger.warning(
                        "RPC error %s while updating price: %s",
                        err.status,
                        err.message,
                    )
                except Exception:  # pragma: no cover - unexpected errors
                    logger.exception("Unexpected error updating price")
        await asyncio.sleep(0)


async def _risk_check(_session: aiohttp.ClientSession | None, intent: Dict[str, Any]) -> bool:
    # Run the deterministic risk workflow and optionally consult an LLM
    # for a human-style approval decision.
    client = await _get_client()
    wf_id = f"risk-{secrets.token_hex(8)}"
    try:
        handle = await client.start_workflow(
            PreTradeRiskCheck.run,
            args=[wf_id, [intent]],
            id=wf_id,
            task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
        )
        result = await handle.result()
    except Exception as exc:
        logger.error("Risk workflow failed: %s", exc)
        return False

    if result.get("status") != "APPROVED":
        return False

    status = await _get_ledger_status(client)

    if openai is None:
        return True

    prompt = (
        "Current cash: ${cash:.2f}\n"
        "Positions: {positions}\n"
        "Entry prices: {entry_prices}\n"
        "Intent: {intent}\n"
        "Risk result: {risk}\n"
        "Respond with APPROVE or REJECT followed by a colon and the reason."
    ).format(
        cash=status.get("cash", 0.0),
        positions=status.get("positions"),
        entry_prices=status.get("entry_prices"),
        intent=intent,
        risk=result,
    )

    logger.info("Sending prompt to ChatGPT:\n%s", prompt)

    try:
        client_ai = openai.AsyncOpenAI()
        resp = await client_ai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        reply = resp.choices[0].message.content.strip()
        if ":" in reply:
            decision_word, reason = reply.split(":", 1)
            decision = decision_word.strip().upper()
            reason = reason.strip()
        else:
            decision = reply.strip().upper()
            reason = ""
        logger.info("ChatGPT replied: %s - %s", decision, reason)
        return "APPROVE" in decision
    except Exception as exc:  # pragma: no cover - network errors
        logger.error("LLM decision failed: %s", exc)
        return True


async def _broadcast_intent(client: Client, intent: Dict[str, Any]) -> None:
    """Publish ``intent`` to the IntentBus workflow."""
    handle = client.get_workflow_handle(INTENT_BUS_WF_ID)
    await handle.signal("publish", intent)


async def _poll_signals(session: aiohttp.ClientSession) -> None:
    cursor = 0
    while not STOP_EVENT.is_set():
        url = f"http://{MCP_HOST}:{MCP_PORT}/signal/strategy_signal"
        events = await _fetch(session, url, params={"after": cursor})
        if not events:
            await asyncio.sleep(1)
            continue
        for evt in events:
            symbol = evt.get("symbol")
            side = evt.get("side")
            ts = evt.get("ts")
            if not symbol or not side:
                continue
            try:
                client = await _get_client()
                await _ensure_workflow(client)
                handle = client.get_workflow_handle(ENSEMBLE_WF_ID)
                price = await handle.query("get_price", symbol) or 0.0
                intent = {
                    "symbol": symbol,
                    "side": side,
                    "qty": INTENT_QTY,
                    "price": price,
                    "ts": ts,
                }
                approved = await _risk_check(session, intent)
                if approved:
                    enqueue_intent(intent)
                    await _broadcast_intent(client, intent)
                    await handle.signal("record_intent", intent)
                    logger.info(
                        "Enqueued intent:\n%s",
                        format_log(intent),
                    )
                else:
                    logger.info(
                        "Intent rejected:\n%s",
                        format_log(intent),
                    )
                cursor = max(cursor, ts)
            except RPCError as err:  # pragma: no cover - network failures
                logger.warning(
                    "RPC error %s while processing intent: %s",
                    err.status,
                    err.message,
                )
            except Exception:  # pragma: no cover - unexpected errors
                logger.exception("Unexpected error processing intent")
        await asyncio.sleep(0)


async def main() -> None:
    print_banner(
        "Ensemble Agent",
        "Aggregate signals and broadcast intents",
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, STOP_EVENT.set)

    timeout = aiohttp.ClientTimeout(total=30)
    client = await _get_client()
    await _ensure_workflow(client)
    await _ensure_intent_bus(client)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        vec_task = asyncio.create_task(_poll_vectors(session))
        sig_task = asyncio.create_task(_poll_signals(session))
        await STOP_EVENT.wait()
        vec_task.cancel()
        sig_task.cancel()
        await asyncio.gather(vec_task, sig_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass
