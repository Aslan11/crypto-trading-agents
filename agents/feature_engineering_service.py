"""Service that computes feature vectors from raw market ticks."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any
import json

import aiohttp
from temporalio.client import Client

from agents.utils import print_banner, format_log


def _add_project_root_to_path() -> None:
    """Ensure the repository root is on ``sys.path`` for imports."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_add_project_root_to_path()

# Import ComputeFeatureVector lazily inside the helper that starts the workflow
# to avoid exposing the workflow definition at module import time. This prevents
# duplicate workflow registration when the worker scans modules for definitions.

__all__ = ["main"]

TEMPORAL_CLIENT: Client | None = None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
LOG_EVERY = int(os.environ.get("LOG_EVERY", "10"))
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "mcp-tools")
FEATURE_WINDOW_SEC = int(os.environ.get("VECTOR_WINDOW_SEC", "300"))
VECTOR_CONTINUE_EVERY = int(os.environ.get("VECTOR_CONTINUE_EVERY", "3600"))
VECTOR_HISTORY_LIMIT = int(os.environ.get("VECTOR_HISTORY_LIMIT", "9000"))

STOP_EVENT = asyncio.Event()
TASKS: set[asyncio.Task[Any]] = set()


async def _get_client() -> Client:
    global TEMPORAL_CLIENT
    if TEMPORAL_CLIENT is None:
        TEMPORAL_CLIENT = await Client.connect(
            TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE
        )
    return TEMPORAL_CLIENT




async def _signal_tick(client: Client, symbol: str, tick: dict) -> None:
    # Import here to avoid registering the workflow twice when the worker scans
    # modules for definitions.
    from tools.feature_engineering import ComputeFeatureVector

    wf_id = f"feature-{symbol.replace('/', '-') }"
    await client.start_workflow(
        ComputeFeatureVector.run,
        id=wf_id,
        task_queue=TASK_QUEUE,
        start_signal="market_tick",
        start_signal_args=[tick],
        args=[symbol, FEATURE_WINDOW_SEC, VECTOR_CONTINUE_EVERY, VECTOR_HISTORY_LIMIT],
    )


async def _poll_ticks(session: aiohttp.ClientSession, client: Client) -> None:
    # Track the last processed timestamp so we only fetch new ticks
    cursor = 0
    # Basic exponential backoff when the MCP server returns no data
    backoff = 1
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/market_tick"
    headers = {"Accept": "text/event-stream"}

    while not STOP_EVENT.is_set():
        try:
            async with session.get(url, params={"after": cursor}, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("Signal stream error %s", resp.status)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue

                backoff = 1
                while not STOP_EVENT.is_set():
                    line = await resp.content.readline()
                    if not line:
                        break
                    text = line.decode().strip()
                    if not text or not text.startswith("data:"):
                        continue
                    try:
                        tick = json.loads(text[5:].strip())
                    except Exception:
                        continue
                    symbol = tick.get("symbol")
                    ts = tick.get("ts")
                    if symbol is None or ts is None:
                        continue
                    task = asyncio.create_task(_signal_tick(client, symbol, tick))
                    TASKS.add(task)
                    task.add_done_callback(TASKS.discard)
                    cursor = max(cursor, ts)
        except Exception as exc:
            logger.error("Signal stream failed: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _shutdown() -> None:
    for t in list(TASKS):
        t.cancel()
    await asyncio.gather(*TASKS, return_exceptions=True)


async def main() -> None:
    """Run the feature engineering service."""

    print_banner(
        "Feature Engineering Service",
        "Compute feature vectors",
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, STOP_EVENT.set)

    # Streams of ticks and vectors may be long-lived; disable request timeout
    timeout = aiohttp.ClientTimeout(total=None)
    temporal_client = await _get_client()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tick_task = asyncio.create_task(_poll_ticks(session, temporal_client))
        await STOP_EVENT.wait()
        tick_task.cancel()
        await asyncio.gather(tick_task, return_exceptions=True)
        await _shutdown()


if __name__ == "__main__":
    asyncio.run(main())
