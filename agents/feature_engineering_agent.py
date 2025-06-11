from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

import aiohttp

__all__ = ["get_latest_vector", "main"]

# Global in-memory feature store
FEATURE_STORE: dict[tuple[str, int], dict] = {}
_FEATURE_LOCK = asyncio.Lock()
_VECTOR_COUNT = 0

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTC/USDT").split(",")]
LOG_EVERY = int(os.environ.get("LOG_EVERY", "10"))

STOP_EVENT = asyncio.Event()
TASKS: set[asyncio.Task[Any]] = set()


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


async def _start_compute(session: aiohttp.ClientSession, symbol: str) -> tuple[str, str] | None:
    url = f"http://{MCP_HOST}:{MCP_PORT}/tools/ComputeFeatureVector"
    payload = {"symbol": symbol, "window_sec": 60}
    backoff = 1
    while not STOP_EVENT.is_set():
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 202:
                    data = await resp.json()
                    return data["workflow_id"], data["run_id"]
                logger.warning("Tool start error %s", resp.status)
        except Exception as exc:
            logger.error("Error starting workflow: %s", exc)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
    return None


async def _poll_workflow(
    session: aiohttp.ClientSession, symbol: str, ts: int, wf_id: str, run_id: str
) -> None:
    url = f"http://{MCP_HOST}:{MCP_PORT}/workflow/{wf_id}/{run_id}"
    backoff = 1
    while not STOP_EVENT.is_set():
        await asyncio.sleep(1)
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    status = data.get("status")
                    if status == "COMPLETED":
                        result = data.get("result")
                        if isinstance(result, dict):
                            await _store_vector(symbol, ts, result)
                        else:
                            logger.error("Workflow %s completed without result", wf_id)
                        return
                    if status != "RUNNING":
                        logger.error("Workflow %s ended with %s", wf_id, status)
                        return
                    backoff = 1
                    continue
                logger.warning("Workflow poll error %s", resp.status)
        except Exception as exc:
            logger.error("Error polling workflow %s: %s", wf_id, exc)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def _handle_tick(session: aiohttp.ClientSession, symbol: str, ts: int) -> None:
    wf = await _start_compute(session, symbol)
    if not wf:
        return
    wf_id, run_id = wf
    await _poll_workflow(session, symbol, ts, wf_id, run_id)


async def _poll_ticks(session: aiohttp.ClientSession) -> None:
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
                task = asyncio.create_task(_handle_tick(session, symbol, ts))
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
    async with aiohttp.ClientSession(timeout=timeout) as session:
        poll_task = asyncio.create_task(_poll_ticks(session))
        await STOP_EVENT.wait()
        poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)
        await _shutdown()


if __name__ == "__main__":
    asyncio.run(main())
