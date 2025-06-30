"""Utility functions for analyzing ticker performance."""

from __future__ import annotations

import logging
import os
from typing import Dict, List

from temporalio import workflow

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def compute_performance(ticks: List[dict]) -> Dict[str, float]:
    """Return cumulative and average daily returns for a sequence of ticks."""
    if len(ticks) < 2:
        return {"cumulative_return": 0.0, "average_daily_return": 0.0}

    ticks = sorted(ticks, key=lambda t: t.get("ts", 0))
    prices: List[float] = []
    ts_list: List[int] = []
    for t in ticks:
        data = t.get("data", {})
        if "last" in data:
            price = float(data["last"])
        elif {"bid", "ask"}.issubset(data):
            price = (float(data["bid"]) + float(data["ask"])) / 2
        else:
            continue
        prices.append(price)
        ts_list.append(int(t.get("ts", 0)))

    if len(prices) < 2:
        return {"cumulative_return": 0.0, "average_daily_return": 0.0}

    cumulative = (prices[-1] - prices[0]) / prices[0] * 100
    days = (ts_list[-1] - ts_list[0]) / 86400
    avg_daily = cumulative / days if days else cumulative
    return {"cumulative_return": cumulative, "average_daily_return": avg_daily}


@workflow.defn
class GetHistoricalPerformance:
    """Workflow wrapper that computes performance from provided ticks."""

    @workflow.run
    async def run(self, ticks: List[dict]) -> Dict[str, float]:
        return compute_performance(ticks)
