"""Feature engineering tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import numpy as np
import pandas as pd
from temporalio import activity, workflow
import aiohttp
import os
import asyncio
import logging

SMA_SHORT_MIN = int(os.environ.get("SMA_SHORT_MIN", "1"))
SMA_LONG_MIN = int(os.environ.get("SMA_LONG_MIN", "5"))
VECTOR_WINDOW_SEC = int(os.environ.get("VECTOR_WINDOW_SEC", str(SMA_LONG_MIN * 60)))
VECTOR_CONTINUE_EVERY = int(os.environ.get("VECTOR_CONTINUE_EVERY", "3600"))
VECTOR_HISTORY_LIMIT = int(os.environ.get("VECTOR_HISTORY_LIMIT", "9000"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")


@activity.defn
def compute_indicators(ticks: List[dict]) -> dict:
    """Return standard indicators for given ticks.

    Parameters
    ----------
    ticks:
        Sequence of ccxt ticker dictionaries sorted from oldest to newest.

    Returns
    -------
    dict
        JSON-serialisable mapping with keys ``ts`` (int seconds), ``mid``,
        ``sma1``, ``sma5``, ``ret1m``, ``vol1m``.
    """
    if not ticks:
        raise ValueError("ticks list cannot be empty")

    df = pd.DataFrame(ticks)
    if "timestamp" not in df:
        raise KeyError("ticks must include 'timestamp' field")

    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.sort_values("ts", inplace=True)

    if {"bid", "ask"}.issubset(df.columns):
        df["mid"] = (df["bid"] + df["ask"]) / 2
    else:
        df["mid"] = df.get("last")

    df.set_index("ts", inplace=True)

    sma1 = df["mid"].rolling(f"{SMA_SHORT_MIN}min").mean().iloc[-1]
    sma5 = df["mid"].rolling(f"{SMA_LONG_MIN}min").mean().iloc[-1]
    vol1m = df["mid"].rolling(f"{SMA_SHORT_MIN}min").std().iloc[-1]

    last_ts = df.index[-1]
    mid_price = df["mid"].iloc[-1]
    prev_series = df["mid"].loc[: last_ts - pd.Timedelta(minutes=SMA_SHORT_MIN)]
    if prev_series.empty:
        ret1m = np.nan
    else:
        prev_price = prev_series.iloc[-1]
        ret1m = (mid_price - prev_price) / prev_price * 100

    return {
        "ts": int(last_ts.timestamp()),
        "mid": float(mid_price),
        "sma1": None if np.isnan(sma1) else float(sma1),
        "sma5": None if np.isnan(sma5) else float(sma5),
        "ret1m": None if np.isnan(ret1m) else float(ret1m),
        "vol1m": None if np.isnan(vol1m) else float(vol1m),
    }


@activity.defn
async def record_vector(vector: dict) -> None:
    """Send feature vector payload to MCP server signal log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=vector)
        except Exception as exc:
            logger.error("Failed to record vector: %s", exc)


@workflow.defn
class ComputeFeatureVector:
    """Continuously compute feature vectors from ``market_tick`` signals."""

    def __init__(self) -> None:
        self.symbol = ""
        self.window_sec = VECTOR_WINDOW_SEC
        self._ticks: List[dict] = []
        # Use an asyncio.Event for signalling between the signal handler and
        # the main workflow loop. Temporal workflows should avoid waiting on
        # events directly, so we will wait via ``workflow.wait_condition``.
        self._event = asyncio.Event()

    @workflow.query
    def historical_ticks(self, since_ts: int = 0) -> list[dict]:
        """Return stored ticks newer than ``since_ts``.

        Parameters
        ----------
        since_ts:
            Unix timestamp in seconds. Only ticks at or after this time are
            returned.
        """

        ticks: list[dict] = []
        for t in self._ticks:
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
        return ticks

    @workflow.signal
    def market_tick(self, tick: dict) -> None:
        if tick.get("symbol") != self.symbol:
            return
        data = tick.get("data", {})
        self._ticks.append(data)
        self._event.set()

    @workflow.run
    async def run(
        self,
        symbol: str,
        window_sec: int = VECTOR_WINDOW_SEC,
        continue_every: int = VECTOR_CONTINUE_EVERY,
        history_limit: int = VECTOR_HISTORY_LIMIT,
    ) -> None:
        self.symbol = symbol
        self.window_sec = window_sec
        cycles = 0
        while True:
            # Wait for a new tick to be signalled via the event. We use
            # ``workflow.wait_condition`` so that the wait is deterministic in
            # Temporal's workflow environment.
            await workflow.wait_condition(lambda: self._event.is_set())
            self._event.clear()

            now = workflow.now()
            since = now - timedelta(seconds=self.window_sec)
            pruned: List[dict] = []
            for t in self._ticks:
                ts_ms = t.get("timestamp")
                if ts_ms is None:
                    continue
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                if ts >= since:
                    pruned.append(t)
            self._ticks = pruned

            if not self._ticks:
                # Wait for a new tick if all have expired
                continue

            vector = await workflow.execute_activity(
                compute_indicators,
                self._ticks,
                schedule_to_close_timeout=timedelta(seconds=10),
            )

            payload = {"symbol": self.symbol, "ts": vector["ts"], "data": vector}
            await workflow.execute_activity(
                record_vector,
                payload,
                schedule_to_close_timeout=timedelta(seconds=5),
            )
            cycles += 1
            hist_len = workflow.info().get_current_history_length()
            if hist_len >= history_limit or workflow.info().is_continue_as_new_suggested():
                await workflow.continue_as_new(
                    args=[
                        symbol,
                        window_sec,
                        continue_every,
                        history_limit,
                    ]
                )
            if cycles >= continue_every:
                await workflow.continue_as_new(
                    args=[
                        symbol,
                        window_sec,
                        continue_every,
                        history_limit,
                    ]
                )
