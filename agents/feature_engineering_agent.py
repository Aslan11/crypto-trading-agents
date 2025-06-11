from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

import aiohttp
from temporalio.client import Client
from tools.feature_engineering import ComputeFeatureVector

__all__ = ["get_latest_vector", "main"]

# Global in-memory feature store
FEATURE_STORE: dict[tuple[str, int], dict] = {}
_FEATURE_LOCK = asyncio.Lock()
_VECTOR_COUNT = 0

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC/USD").split(",")]
LOG_EVERY = int(os.environ.get("LOG_EVERY", "10"))
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "mcp-tools")

STOP_EVENT = asyncio.Event()
TASKS: set[asyncio.Task[Any]] = set()


async def _poll_vectors(session: aiohttp.ClientSession) -> None:
    cursor = 0
    backoff = 1
    while not STOP_EVENT.is_set():
        url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
        try:
            async with session.get(url, params={"after": cursor}) as resp:
                if resp.status == 200:
                    events = await resp.json()
                    backoff = 1
                else:
                    logger.warning("Vector poll error %s", resp.status)
                    events = []
        except Exception as exc:
            logger.error("Vector poll failed: %s", exc)
            events = []
        if not events:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        for evt in events:
            symbol = evt.get("symbol")
            ts = evt.get("ts")
            data = evt.get("data")
            if symbol is None or ts is None or not isinstance(data, dict):
                continue
            await _store_vector(symbol, ts, data)
            cursor = max(cursor, ts)


async def get_latest_vector(symbol: str) -> dict | None:
    """Return the most recent feature vector for ``symbol`` if available."""

    async with _FEATURE_LOCK:
        matches = [(k, v) for k, v in FEATURE_STORE.items() if k[0] == symbol]
        if not matches:
            return None
        (sym, ts), vec = max(matches, key=lambda kv: kv[0][1])
        return vec


async def _store_vector(symbol: str, ts: int, vector: dict) -> None:
    global _VECTOR_COUNT

    async with _FEATURE_LOCK:
        FEATURE_STORE[(symbol, ts)] = vector
        _VECTOR_COUNT += 1
        count = _VECTOR_COUNT
    if count % LOG_EVERY == 0:
        logger.info(
            "Stored vector %d for %s @ %d: %s", count, symbol, ts, vector
        )


async def _signal_tick(client: Client, symbol: str, tick: dict) -> None:
    wf_id = f"feature-{symbol.replace('/', '-') }"
    await client.signal_with_start_workflow(
        ComputeFeatureVector.run,
        id=wf_id,
        task_queue=TASK_QUEUE,
        signal="market_tick",
        signal_args=[tick],
        args=[symbol, 60],
    )


async def _poll_ticks(session: aiohttp.ClientSession, client: Client) -> None:
    cursor = 0
    backoff = 1
    while not STOP_EVENT.is_set():
        url = f"http://{MCP_HOST}:{MCP_PORT}/signal/market_tick"
        try:
            async with session.get(url, params={"after": cursor}) as resp:
                if resp.status == 200:
                    ticks = await resp.json()
                    backoff = 1
                else:
                    logger.warning("Signal poll error %s", resp.status)
                    ticks = []
        except Exception as exc:
            logger.error("Signal poll failed: %s", exc)
            ticks = []
        if not ticks:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        for tick in ticks:
            symbol = tick.get("symbol")
            ts = tick.get("ts")
            if symbol is None or ts is None:
                continue
            if symbol in SYMBOLS:
                task = asyncio.create_task(_signal_tick(client, symbol, tick))
                TASKS.add(task)
                task.add_done_callback(TASKS.discard)
            cursor = max(cursor, ts)
        await asyncio.sleep(1)


async def _shutdown() -> None:
    for t in list(TASKS):
        t.cancel()
    await asyncio.gather(*TASKS, return_exceptions=True)


async def main() -> None:
    """Run the feature engineering agent."""

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, STOP_EVENT.set)

    timeout = aiohttp.ClientTimeout(total=30)
    temporal_client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)
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
