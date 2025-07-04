"""Market data tools and workflows."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import os
from typing import Any, List, Dict

import aiohttp
from pydantic import BaseModel
from temporalio import activity, workflow


class MarketTick(BaseModel):
    """Ticker payload sent to child workflows."""

    exchange: str
    symbol: str
    data: dict[str, Any]


MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
# Automatically continue the workflow periodically to avoid unbounded history
STREAM_CONTINUE_EVERY = int(os.environ.get("STREAM_CONTINUE_EVERY", "3600"))
STREAM_HISTORY_LIMIT = int(os.environ.get("STREAM_HISTORY_LIMIT", "9000"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)



COINBASE_ID = "coinbaseexchange"


@activity.defn
async def fetch_ticker(symbol: str) -> dict[str, Any]:
    """Return the latest ticker for ``symbol`` from Coinbase."""
    import ccxt.async_support as ccxt
    client = ccxt.coinbaseexchange()
    try:
        data = await client.fetch_ticker(symbol)
        return MarketTick(exchange=COINBASE_ID, symbol=symbol, data=data).model_dump()
    except Exception as exc:
        logger.error("Failed to fetch ticker %s:%s - %s", COINBASE_ID, symbol, exc)
        raise
    finally:
        await client.close()


@activity.defn
async def record_tick(tick: dict) -> None:
    """Send tick payload to MCP server signal log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/market_tick"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=tick)
        except Exception as exc:
            logger.error("Failed to record tick: %s", exc)


@workflow.defn
class SubscribeCEXStream:
    """Periodically fetch tickers and broadcast them to children."""

    def __init__(self) -> None:
        self.symbols: list[str] = []
        self._history: Dict[str, list[dict]] = {}

    @workflow.query
    def historical_ticks(self, symbol: str, since_ts: int = 0) -> list[dict]:
        """Return stored ticks for ``symbol`` newer than ``since_ts``."""
        ticks: list[dict] = []
        for t in self._history.get(symbol, []):
            ts_ms = t.get("timestamp")
            if ts_ms is None:
                continue
            ts = int(ts_ms / 1000)
            if ts < since_ts:
                continue
            if "last" in t:
                price = float(t["last"])
            elif {"bid", "ask"}.issubset(t):
                price = (float(t["bid"]) + float(t["ask"])) / 2
            else:
                continue
            ticks.append({"ts": ts, "price": price})
        ticks.sort(key=lambda x: x["ts"])
        logger.info("historical_ticks returning %d items for %s", len(ticks), symbol)
        return ticks

    @workflow.run
    async def run(
        self,
        symbols: List[str],
        interval_sec: int = 1,
        max_cycles: int | None = None,
        continue_every: int = STREAM_CONTINUE_EVERY,
        history_limit: int = STREAM_HISTORY_LIMIT,
        history: Dict[str, list[dict]] | None = None,
    ) -> None:
        """Stream tickers indefinitely, continuing as new periodically."""
        self.symbols = list(symbols)
        if history is not None:
            self._history = {s: list(history.get(s, [])) for s in symbols}
        else:
            self._history = {s: [] for s in symbols}
        cycles = 0
        while True:
            tickers = await asyncio.gather(
                *[
                    workflow.execute_activity(
                        fetch_ticker,
                        args=[symbol],
                        schedule_to_close_timeout=timedelta(seconds=10),
                    )
                    for symbol in symbols
                ]
            )
            wf_id = workflow.info().workflow_id
            for ticker in tickers:
                symbol = ticker.get("symbol")
                data = ticker.get("data", {})
                if symbol:
                    hist = self._history.setdefault(symbol, [])
                    hist.append(data)
                    if len(hist) > 1000:
                        self._history[symbol] = hist[-1000:]
                await workflow.execute_activity(
                    record_tick,
                    {"workflow_id": wf_id, **ticker},
                    schedule_to_close_timeout=timedelta(seconds=5),
                )
                if hasattr(workflow, "signal_child_workflows"):
                    await workflow.signal_child_workflows("market_tick", ticker)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            hist_len = workflow.info().get_current_history_length()
            if hist_len >= history_limit or workflow.info().is_continue_as_new_suggested():
                await workflow.continue_as_new(
                    args=[
                        symbols,
                        interval_sec,
                        max_cycles,
                        continue_every,
                        history_limit,
                        self._history,
                    ]
                )
            if cycles >= continue_every:
                await workflow.continue_as_new(
                    args=[
                        symbols,
                        interval_sec,
                        max_cycles,
                        continue_every,
                        history_limit,
                        self._history,
                    ]
                )
            await workflow.sleep(interval_sec)
