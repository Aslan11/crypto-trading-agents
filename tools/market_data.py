"""Market data tools and workflows."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import os
from typing import Any, List

import aiohttp

from pydantic import BaseModel
from temporalio import activity, workflow
from tools.feature_engineering import signal_compute_vector


class MarketTick(BaseModel):
    """Ticker payload sent to child workflows."""

    exchange: str
    symbol: str
    data: dict[str, Any]


# MCP server endpoint for recording tick signals
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
    """Send tick payload to the MCP server signal log."""
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

    @workflow.run
    async def run(
        self,
        symbols: List[str],
        interval_sec: int = 1,
        max_cycles: int | None = None,
        continue_every: int = STREAM_CONTINUE_EVERY,
        history_limit: int = STREAM_HISTORY_LIMIT,
    ) -> None:
        """Stream tickers indefinitely, continuing as new periodically."""
        # Feature vector workflows are started lazily via an activity

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
            tasks = []
            for t in tickers:
                tasks.append(
                    workflow.start_activity(
                        record_tick,
                        t,
                        schedule_to_close_timeout=timedelta(seconds=5),
                    )
                )
                tasks.append(
                    workflow.start_activity(
                        signal_compute_vector,
                        args=[t.get("symbol"), t],
                        schedule_to_close_timeout=timedelta(seconds=5),
                    )
                )
            await asyncio.gather(*tasks)
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
                    ]
                )
            await workflow.sleep(interval_sec)
