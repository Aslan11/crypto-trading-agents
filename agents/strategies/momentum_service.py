"""Simple momentum trading strategy service."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Set


import aiohttp


def _add_project_root_to_path() -> None:
    """Ensure repository root is on ``sys.path`` for imports."""
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_add_project_root_to_path()
from agents.feature_engineering_service import subscribe_vectors  # noqa: E402
from agents.workflows import MomentumWorkflow  # noqa: E402
from agents.utils import print_banner, format_log
from temporalio.client import Client  # noqa: E402
from temporalio.service import RPCError, RPCStatusCode  # noqa: E402


logger = logging.getLogger(__name__)

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
MCP_PATH = os.environ.get(
    "MCP_BASE_PATH",
    os.environ.get("FASTMCP_STREAMABLE_HTTP_PATH", "/mcp"),
).rstrip("/")
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "30"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "0.5"))

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


def _wf_id(symbol: str | None = None) -> str:
    if symbol is None:
        return MOMENTUM_WF_ID
    return f"{MOMENTUM_WF_ID}-{symbol.replace('/', '-')}"


async def _ensure_workflow(client: Client, symbol: str | None = None) -> None:
    wf_id = _wf_id(symbol)
    handle = client.get_workflow_handle(wf_id)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                MomentumWorkflow.run,
                id=wf_id,
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
    base = f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"
    url = f"{base}/tools/evaluate_strategy_momentum"
    payload = {"signal": signal_payload}
    headers = {"Accept": "application/json, text/event-stream"}
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
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
        await asyncio.sleep(POLL_INTERVAL)
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


async def _run_tool(session: aiohttp.ClientSession, payload: dict) -> None:
    """Start momentum evaluation tool and record the result asynchronously."""
    wf = await _start_tool(session, payload)
    if not wf:
        logger.warning("Failed to start momentum tool")
        return
    wf_id, run_id = wf
    result = await _poll_tool(session, wf_id, run_id)
    logger.info(
        "Tool completed with result:\n%s",
        format_log(result),
    )
    if result:
        await _record_signal(session, result)


async def main() -> None:
    """Run the momentum strategy service."""
    print_banner(
        "Momentum Strategy Service",
        "Emit buy/sell signals via SMA crossover",
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _handle_sigint)

    async def _subscribe_all_vectors() -> AsyncIterator[tuple[str, dict]]:
        cursor = 0
        url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not STOP_EVENT.is_set():
                try:
                    async with session.get(url, params={"after": cursor}) as resp:
                        if resp.status == 200:
                            events = await resp.json()
                        else:
                            logger.warning("Vector poll error %s", resp.status)
                            events = []
                except Exception as exc:
                    logger.error("Vector poll failed: %s", exc)
                    events = []

                if not events:
                    await asyncio.sleep(1)
                    continue

                for evt in events:
                    if STOP_EVENT.is_set():
                        return
                    sym = evt.get("symbol")
                    ts = evt.get("ts")
                    data = evt.get("data")
                    if sym is None or ts is None or not isinstance(data, dict):
                        continue
                    cursor = max(cursor, ts)
                    yield sym, data

    async def _run(session: aiohttp.ClientSession) -> None:
        client = await _get_client()
        handles: dict[str, Any] = {}
        last_sig: dict[str, int] = {}
        tasks: Set[asyncio.Task] = set()

        async for symbol, vector in _subscribe_all_vectors():
            if symbol not in handles:
                await _ensure_workflow(client, symbol)
                handles[symbol] = client.get_workflow_handle(_wf_id(symbol))
                last_sig[symbol] = 0
                logger.info("Momentum service now watching %s", symbol)

            handle = handles[symbol]
            await handle.signal("add_vector", vector)
            sig = await handle.query("next_signal", last_sig[symbol])
            if not sig:
                continue
            last_sig[symbol] = sig["ts"]
            side = sig["side"]
            payload = {"symbol": symbol, "side": side, "ts": last_sig[symbol]}
            logger.info("Emitting %s signal for %s", side, symbol)
            task = asyncio.create_task(_run_tool(session, payload))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        if tasks:
            await asyncio.gather(*tasks)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _run(session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - entrypoint convenience
        pass
