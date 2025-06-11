"""Feature engineering tools."""

from __future__ import annotations

from datetime import timedelta
from typing import List

import numpy as np
import pandas as pd
from temporalio import activity, workflow


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

    sma1 = df["mid"].rolling("1min").mean().iloc[-1]
    sma5 = df["mid"].rolling("5min").mean().iloc[-1]
    vol1m = df["mid"].rolling("1min").std().iloc[-1]

    last_ts = df.index[-1]
    mid_price = df["mid"].iloc[-1]
    prev_series = df["mid"].loc[: last_ts - pd.Timedelta(minutes=1)]
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


@workflow.defn
class ComputeFeatureVector:
    """Build a feature vector from recent ``market_tick`` signals."""

    @workflow.run
    async def run(self, symbol: str, window_sec: int = 60) -> dict:
        """Gather tick history and compute indicators.

        Parameters
        ----------
        symbol:
            Market symbol whose ticks should be considered.
        window_sec:
            How many seconds back to fetch ``market_tick`` signals.
        """
        since = workflow.now() - timedelta(seconds=window_sec)
        ticks: List[dict] = []
        get_history = getattr(workflow, "get_signal_history", None)
        if get_history:
            async for evt in get_history("market_tick", start_time=since):
                tick = evt.args[0] if isinstance(evt.args, list) else evt.args
                if isinstance(tick, dict) and tick.get("symbol") == symbol:
                    data = tick.get("data", {})
                    ticks.append(data)

        return await workflow.execute_activity(
            compute_indicators,
            ticks,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
