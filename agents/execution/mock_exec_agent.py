from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime
from decimal import Decimal

import aiohttp
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from agents.workflows import ExecutionLedgerWorkflow

try:
    from agents.shared_bus import APPROVED_INTENT_QUEUE
except Exception:  # pragma: no cover - fallback for missing module
    APPROVED_INTENT_QUEUE: asyncio.Queue[dict] = asyncio.Queue()


MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

LEDGER_WF_ID = "mock-ledger"
TEMPORAL_CLIENT: Client | None = None
STOP_EVENT = asyncio.Event()


async def _get_client() -> Client:
    global TEMPORAL_CLIENT
    if TEMPORAL_CLIENT is None:
        address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
        TEMPORAL_CLIENT = await Client.connect(address, namespace=namespace)
    return TEMPORAL_CLIENT


async def _ensure_workflow(client: Client) -> None:
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


async def _fetch(
    session: aiohttp.ClientSession, url: str, params: dict | None = None
) -> list[dict] | None:
    try:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.warning("GET %s returned %s", url, resp.status)
    except Exception as exc:  # pragma: no cover - network errors
        logger.error("GET %s failed: %s", url, exc)
    return None


async def _update_ledger(fill: dict) -> None:
    client = await _get_client()
    await _ensure_workflow(client)
    handle = client.get_workflow_handle(LEDGER_WF_ID)
    await handle.signal("record_fill", fill)


async def _place_order(session: aiohttp.ClientSession, intent: dict) -> None:
    qty = Decimal(str(intent["qty"]))
    price = Decimal(str(intent["price"]))
    cost = qty * price

    client = await _get_client()
    await _ensure_workflow(client)
    handle = client.get_workflow_handle(LEDGER_WF_ID)
    cash = await handle.query("get_cash")

    if intent["side"] == "BUY" and Decimal(str(cash)) < cost:
        logger.warning("INSUFFICIENT_FUNDS for %s", intent)
        return

    try:
        resp = await session.post(
            f"http://{MCP_HOST}:{MCP_PORT}/tools/PlaceMockOrder",
            json={"intent": intent},
        )
        data = await resp.json()
        wf_id = data["workflow_id"]
        run_id = data["run_id"]
    except Exception as exc:  # pragma: no cover - network errors
        logger.error("Failed to start workflow: %s", exc)
        return

    fill = None
    while not STOP_EVENT.is_set():
        try:
            status_resp = await session.get(
                f"http://{MCP_HOST}:{MCP_PORT}/workflow/{wf_id}/{run_id}"
            )
            payload = await status_resp.json()
            status = payload.get("status")
            if status == "COMPLETED":
                fill = payload.get("result")
                break
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("Status poll failed: %s", exc)
            await asyncio.sleep(0.5)
            continue
        await asyncio.sleep(0.5)

    if fill:
        await _update_ledger(fill)


async def _poll_intents(session: aiohttp.ClientSession) -> None:
    cursor = 0
    backoff = 1
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/approved_intent"
    while not STOP_EVENT.is_set():
        events = await _fetch(session, url, params={"after": cursor}) or []
        if not events:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        for intent in events:
            ts = intent.get("ts")
            if ts is None:
                continue
            cursor = max(cursor, ts)
            await _place_order(session, intent)
        await asyncio.sleep(0)


async def _run() -> None:
    timeout = aiohttp.ClientTimeout(total=10)
    client = await _get_client()
    await _ensure_workflow(client)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        poll_task = asyncio.create_task(_poll_intents(session))
        try:
            while not STOP_EVENT.is_set():
                try:
                    intent = await asyncio.wait_for(APPROVED_INTENT_QUEUE.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                await _place_order(session, intent)
        finally:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)


async def main() -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, STOP_EVENT.set)
    try:
        await _run()
    finally:
        client = await _get_client()
        handle = client.get_workflow_handle(LEDGER_WF_ID)
        pnl = await handle.query("get_pnl")
        logger.info("Workflow P&L=$%.2f", pnl)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - shutdown
        pass

