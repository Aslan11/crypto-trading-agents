from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
from typing import Any, Dict

import aiohttp

from agents.shared_bus import enqueue_intent
from agents.workflows import EnsembleWorkflow
from tools.intent_bus import IntentBus
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
INTENT_QTY = float(os.environ.get("INTENT_QTY", "1"))

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
                    await handle.signal("update_price", symbol, float(price))
                    cursor = max(cursor, ts)
                except RPCError as err:  # pragma: no cover - network failures
                    logger.warning(
                        "RPC error %s while updating price: %s",
                        err.status,
                        err.message,
                    )
        await asyncio.sleep(0)


async def _risk_check(session: aiohttp.ClientSession, intent: Dict[str, Any]) -> bool:
    payload = {"intent_id": secrets.token_hex(8), "intents": [intent]}
    start = await _post(
        session, f"http://{MCP_HOST}:{MCP_PORT}/tools/PreTradeRiskCheck", payload
    )
    if not start:
        return False
    wf_id = start.get("workflow_id")
    run_id = start.get("run_id")
    if not wf_id or not run_id:
        return False
    while not STOP_EVENT.is_set():
        await asyncio.sleep(0.5)
        status = await _fetch(
            session, f"http://{MCP_HOST}:{MCP_PORT}/workflow/{wf_id}/{run_id}"
        )
        if not status:
            continue
        state = status.get("status")
        if state == "COMPLETED":
            result = status.get("result", {})
            return result.get("status") == "APPROVED"
        if state != "RUNNING":
            logger.error("Risk workflow ended with %s", state)
            return False
    return False


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
                    logger.info("Enqueued intent: %s", intent)
                else:
                    logger.info("Intent rejected: %s", intent)
                cursor = max(cursor, ts)
            except RPCError as err:  # pragma: no cover - network failures
                logger.warning(
                    "RPC error %s while processing intent: %s",
                    err.status,
                    err.message,
                )
        await asyncio.sleep(0)


async def main() -> None:
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
