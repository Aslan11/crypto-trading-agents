"""Simple momentum trading strategy service."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import json
from pathlib import Path
from typing import Any, AsyncIterator, Set


import aiohttp
from mcp import ClientSession as MCPClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent


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
MCP_PATH = (
    os.environ.get(
        "MCP_BASE_PATH",
        os.environ.get("FASTMCP_STREAMABLE_HTTP_PATH", "/mcp"),
    ).rstrip("/")
    + "/"
)
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




async def _record_signal(session: aiohttp.ClientSession, payload: dict) -> None:
    """Send strategy signal payload to the MCP server log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/strategy_signal"
    try:
        await session.post(url, json=payload)
    except Exception as exc:  # pragma: no cover - network errors
        logger.error("Failed to record signal: %s", exc)


def _tool_result_data(result: Any) -> Any:
    """Return JSON-friendly data from a tool call result."""
    if isinstance(result, CallToolResult):
        if result.content:
            first = result.content[0]
            if isinstance(first, TextContent):
                try:
                    return json.loads(first.text)
                except Exception:
                    return first.text
        return [
            c.model_dump() if hasattr(c, "model_dump") else c
            for c in result.content
        ]
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result


async def _run_tool(
    http_session: aiohttp.ClientSession,
    mcp_session: MCPClientSession,
    payload: dict,
) -> None:
    """Call the momentum tool via FastMCP and record the result."""
    try:
        result = await mcp_session.call_tool(
            "evaluate_strategy_momentum", {"signal": payload}
        )
    except Exception as exc:
        logger.error("Failed to start momentum tool: %s", exc)
        return

    data = _tool_result_data(result)
    logger.info("Tool completed with result:\n%s", format_log(data))
    if isinstance(data, dict):
        await _record_signal(http_session, data)


async def main() -> None:
    """Run the momentum strategy service."""
    print_banner(
        "Momentum Strategy Service",
        "Emit buy/sell signals via SMA crossover",
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _handle_sigint)

    async def _subscribe_all_vectors(
        session: aiohttp.ClientSession,
    ) -> AsyncIterator[tuple[str, dict]]:
        cursor = 0
        url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
        headers = {"Accept": "text/event-stream"}

        while not STOP_EVENT.is_set():
            try:
                async with session.get(url, params={"after": cursor}, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning("Vector stream error %s", resp.status)
                        await asyncio.sleep(1)
                        continue

                    while not STOP_EVENT.is_set():
                        line = await resp.content.readline()
                        if not line:
                            break
                        text = line.decode().strip()
                        if not text or not text.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(text[5:].strip())
                        except Exception:
                            continue
                        sym = evt.get("symbol")
                        ts = evt.get("ts")
                        data = evt.get("data")
                        if sym is None or ts is None or not isinstance(data, dict):
                            continue
                        cursor = max(cursor, ts)
                        yield sym, data
            except Exception as exc:
                logger.error("Vector stream failed: %s", exc)
                await asyncio.sleep(1)

    async def _run(
        http_session: aiohttp.ClientSession, mcp_session: MCPClientSession
    ) -> None:
        client = await _get_client()
        handles: dict[str, Any] = {}
        last_sig: dict[str, int] = {}
        tasks: Set[asyncio.Task] = set()

        async for symbol, vector in _subscribe_all_vectors(http_session):
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
            task = asyncio.create_task(
                _run_tool(http_session, mcp_session, payload)
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        if tasks:
            await asyncio.gather(*tasks)

    # SSE connections may remain open; disable the default timeout
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        mcp_url = f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"
        async with streamablehttp_client(mcp_url) as (read_stream, write_stream, _):
            async with MCPClientSession(read_stream, write_stream) as mcp_session:
                await mcp_session.initialize()
                await _run(http_session, mcp_session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - entrypoint convenience
        pass
