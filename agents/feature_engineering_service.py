"""Service that computes feature vectors from raw market ticks."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, AsyncIterator
import json

import aiohttp
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from agents.workflows import FeatureStoreWorkflow
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

__all__ = ["get_latest_vector", "subscribe_vectors", "main"]

FEATURE_WF_ID = "feature-store"
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


async def _poll_vectors(session: aiohttp.ClientSession) -> None:
    """Listen for new feature vectors via SSE and store them."""

    cursor = 0
    backoff = 1
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
    headers = {"Accept": "text/event-stream"}

    while not STOP_EVENT.is_set():
        try:
            async with session.get(url, params={"after": cursor}, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("Vector stream error %s", resp.status)
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
                        evt = json.loads(text[5:].strip())
                    except Exception:
                        continue
                    symbol = evt.get("symbol")
                    ts = evt.get("ts")
                    data = evt.get("data")
                    if symbol is None or ts is None or not isinstance(data, dict):
                        continue
                    await _store_vector(symbol, ts, data)
                    cursor = max(cursor, ts)
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("Vector stream failed: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _get_client() -> Client:
    global TEMPORAL_CLIENT
    if TEMPORAL_CLIENT is None:
        TEMPORAL_CLIENT = await Client.connect(
            TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE
        )
    return TEMPORAL_CLIENT


async def _ensure_workflow(client: Client) -> None:
    handle = client.get_workflow_handle(FEATURE_WF_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                FeatureStoreWorkflow.run,
                id=FEATURE_WF_ID,
                task_queue=TASK_QUEUE,
            )
        else:
            raise


async def get_latest_vector(symbol: str) -> dict | None:
    """Return the most recent feature vector for ``symbol`` if available."""

    client = await _get_client()
    handle = client.get_workflow_handle(FEATURE_WF_ID)
    return await handle.query("latest_vector", symbol)


async def subscribe_vectors(symbol: str, *, use_local: bool = False) -> AsyncIterator[dict]:
    """Yield feature vectors for ``symbol`` as they arrive."""

    if use_local:
        last_ts = 0
        client = await _get_client()
        handle = client.get_workflow_handle(FEATURE_WF_ID)
        while not STOP_EVENT.is_set():
            res = await handle.query("next_vector", symbol, last_ts)
            if not res:
                await asyncio.sleep(0.1)
                continue
            last_ts, vec = res
            yield vec
        return

    cursor = 0
    timeout = aiohttp.ClientTimeout(total=30)
    backoff = 1
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
    headers = {"Accept": "text/event-stream"}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not STOP_EVENT.is_set():
            try:
                async with session.get(url, params={"after": cursor}, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning("Vector stream error %s", resp.status)
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
                            evt = json.loads(text[5:].strip())
                        except Exception:
                            continue
                        sym = evt.get("symbol")
                        ts = evt.get("ts")
                        data = evt.get("data")
                        if sym != symbol or ts is None or not isinstance(data, dict):
                            continue
                        cursor = max(cursor, ts)
                        yield data
            except Exception as exc:
                logger.error("Vector stream failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


async def _store_vector(symbol: str, ts: int, vector: dict) -> None:
    client = await _get_client()
    await _ensure_workflow(client)
    handle = client.get_workflow_handle(FEATURE_WF_ID)
    await handle.signal("add_vector", args=[symbol, ts, vector])
    if ts % LOG_EVERY == 0:
        logger.info(
            "Stored vector for %s @ %d:\n%s",
            symbol,
            ts,
            format_log(vector),
        )


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
        "Compute and store feature vectors",
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, STOP_EVENT.set)

    timeout = aiohttp.ClientTimeout(total=30)
    temporal_client = await _get_client()
    await _ensure_workflow(temporal_client)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tick_task = asyncio.create_task(_poll_ticks(session, temporal_client))
        vec_task = asyncio.create_task(_poll_vectors(session))
        await STOP_EVENT.wait()
        tick_task.cancel()
        vec_task.cancel()
        await asyncio.gather(tick_task, vec_task, return_exceptions=True)
        await _shutdown()


if __name__ == "__main__":
    asyncio.run(main())
