"""Simple momentum trading strategy agent."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from datetime import datetime
from typing import AsyncIterator

import aiohttp

try:
    from agents.feature_engineering_agent import subscribe_vectors  # type: ignore
except Exception:  # pragma: no cover - fallback for missing helper
    async def subscribe_vectors(symbol: str) -> AsyncIterator[dict]:
        """Fallback generator fetching feature vectors from MCP."""
        host = os.environ.get("MCP_HOST", "localhost")
        port = os.environ.get("MCP_PORT", "8080")
        url = f"http://{host}:{port}/signal/feature_vector"
        after = 0
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    async with session.get(url, params={"after": after}) as resp:
                        if resp.status != 200:
                            await asyncio.sleep(1)
                            continue
                        events = await resp.json()
                except Exception as exc:  # pragma: no cover - network errors
                    logging.getLogger(__name__).error("Vector poll error: %s", exc)
                    await asyncio.sleep(5)
                    continue
                if not events:
                    await asyncio.sleep(1)
                    continue
                for evt in events:
                    if evt.get("symbol") != symbol:
                        continue
                    after = max(after, evt.get("ts", 0))
                    yield evt.get("data", evt)


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


def _handle_sigint() -> None:
    STOP_EVENT.set()


def _cross(prev: dict, curr: dict) -> str | None:
    """Return 'BUY' or 'SELL' if SMA crossover detected."""
    p1, p5 = prev.get("sma1"), prev.get("sma5")
    c1, c5 = curr.get("sma1"), curr.get("sma5")
    if None in (p1, p5, c1, c5):
        return None
    if p1 < p5 and c1 > c5:
        return "BUY"
    if p1 > p5 and c1 < c5:
        return "SELL"
    return None


async def _start_tool(session: aiohttp.ClientSession, signal_payload: dict) -> tuple[str, str] | None:
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


async def _poll_tool(session: aiohttp.ClientSession, wf_id: str, run_id: str) -> dict | None:
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


async def main() -> None:
    """Run the momentum strategy agent."""
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _handle_sigint)

    logger.info("Momentum agent watching %s", SYMBOL)

    vec_queue: deque[dict] = deque(maxlen=2)
    vector_count = 0
    last_sent = 0

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async for vector in subscribe_vectors(SYMBOL):
            if STOP_EVENT.is_set():
                break
            vec_queue.append(vector)
            vector_count += 1
            if vector_count % 30 == 0:
                logger.info("Processed %d vectors", vector_count)
            if len(vec_queue) < 2:
                continue
            side = _cross(vec_queue[0], vec_queue[1])
            if not side:
                continue
            now_ts = int(datetime.utcnow().timestamp())
            if now_ts - last_sent < COOLDOWN_SEC:
                continue
            signal_payload = {"symbol": SYMBOL, "side": side, "ts": now_ts}
            logger.info("Emitting %s signal", side)
            wf = await _start_tool(session, signal_payload)
            if not wf:
                continue
            wf_id, run_id = wf
            result = await _poll_tool(session, wf_id, run_id)
            logger.info("Tool completed with result: %s", result)
            last_sent = now_ts
            await asyncio.sleep(COOLDOWN_SEC)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - entrypoint convenience
        pass
