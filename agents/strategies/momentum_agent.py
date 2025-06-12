"""Simple momentum trading strategy agent."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from collections import deque
from datetime import datetime

import aiohttp


def _add_project_root_to_path() -> None:
    """Ensure repository root is on ``sys.path`` for imports."""
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_add_project_root_to_path()
from agents.feature_engineering_agent import subscribe_vectors  # noqa: E402
from agents.workflows import MomentumWorkflow
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode


logger = logging.getLogger(__name__)

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
SYMBOL = os.environ.get("SYMBOL", "BTC/USD")
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "30"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)

STOP_EVENT = asyncio.Event()
MOMENTUM_WF_ID = "momentum-agent"
TEMPORAL_CLIENT: Client | None = None


async def _get_client() -> Client:
    global TEMPORAL_CLIENT
    if TEMPORAL_CLIENT is None:
        address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
        TEMPORAL_CLIENT = await Client.connect(address, namespace=namespace)
    return TEMPORAL_CLIENT


async def _ensure_workflow(client: Client) -> None:
    handle = client.get_workflow_handle(MOMENTUM_WF_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                MomentumWorkflow.run,
                id=MOMENTUM_WF_ID,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
                args=[COOLDOWN_SEC],
            )
        else:
            raise


def _handle_sigint() -> None:
    STOP_EVENT.set()


def _cross(prev: dict, curr: dict) -> str | None:
    """Return 'BUY' or 'SELL' if SMA crossover detected."""
    p1, p5 = prev.get("sma1"), prev.get("sma5")
    c1, c5 = curr.get("sma1"), curr.get("sma5")
    if None in (p1, p5, c1, c5):
        logger.debug("Skipping cross due to missing values: %s %s", prev, curr)
        return None
    if p1 < p5 and c1 > c5:
        logger.debug("Detected BUY cross: %s -> %s", prev, curr)
        return "BUY"
    if p1 > p5 and c1 < c5:
        logger.debug("Detected SELL cross: %s -> %s", prev, curr)
        return "SELL"
    logger.debug("No crossover detected: %s -> %s", prev, curr)
    return None


async def _start_tool(
    session: aiohttp.ClientSession, signal_payload: dict
) -> tuple[str, str] | None:
    url = f"http://{MCP_HOST}:{MCP_PORT}/tools/EvaluateStrategyMomentum"
    payload = {"signal": signal_payload, "cooldown_sec": COOLDOWN_SEC}
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status == 202:
                data = await resp.json()
                return data["workflow_id"], data["run_id"]
            logger.warning("Tool start error %s", resp.status)
    except Exception as exc:
        logger.error("Failed to start tool: %s", exc)
    return None


async def _poll_tool(
    session: aiohttp.ClientSession, wf_id: str, run_id: str
) -> dict | None:
    url = f"http://{MCP_HOST}:{MCP_PORT}/workflow/{wf_id}/{run_id}"
    while not STOP_EVENT.is_set():
        await asyncio.sleep(1)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Poll error %s", resp.status)
                    continue
                data = await resp.json()
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("Workflow poll error: %s", exc)
            continue
        status = data.get("status")
        if status == "COMPLETED":
            return data.get("result")
        if status != "RUNNING":
            logger.error("Workflow %s ended with %s", wf_id, status)
            return data.get("result")
    return None


async def _record_signal(session: aiohttp.ClientSession, payload: dict) -> None:
    """Send strategy signal payload to the MCP server log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/strategy_signal"
    try:
        await session.post(url, json=payload)
    except Exception as exc:  # pragma: no cover - network errors
        logger.error("Failed to record signal: %s", exc)


async def main() -> None:
    """Run the momentum strategy agent."""
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _handle_sigint)

    logger.info("Momentum agent watching %s", SYMBOL)

    client = await _get_client()
    await _ensure_workflow(client)
    handle = client.get_workflow_handle(MOMENTUM_WF_ID)
    last_sig = 0

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async for vector in subscribe_vectors(SYMBOL, use_local=False):
            if STOP_EVENT.is_set():
                break
            await handle.signal("add_vector", vector)
            sig = await handle.query("next_signal", last_sig)
            if not sig:
                continue
            last_sig = sig["ts"]
            side = sig["side"]
            signal_payload = {"symbol": SYMBOL, "side": side, "ts": last_sig}
            logger.info("Emitting %s signal", side)
            wf = await _start_tool(session, signal_payload)
            if not wf:
                logger.warning("Failed to start momentum tool")
                continue
            wf_id, run_id = wf
            result = await _poll_tool(session, wf_id, run_id)
            logger.info("Tool completed with result: %s", result)
            if result:
                logger.debug("Recording signal result: %s", result)
                await _record_signal(session, result)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - entrypoint convenience
        pass
