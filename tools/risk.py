"""Pre-trade risk checks and utilities."""

from __future__ import annotations

from datetime import timedelta
from math import sqrt
from typing import List, Dict

from pydantic import BaseModel
from temporalio import activity, workflow


@activity.defn
def value_at_risk(notional: float, confidence: float = 0.99) -> float:
    """Return the simplistic value at risk for ``notional``.

    Uses a fixed daily volatility of 2% and square-root-of-time scaling.
    """
    var = 0.02 * sqrt(1) * notional
    return var


class RiskResult(BaseModel):
    status: str
    reason: str | None = None


@workflow.defn
class PreTradeRiskCheck:
    """Deterministic risk checks before submitting orders."""

    @workflow.run
    async def run(
        self,
        intent_id: str,
        intents: List[Dict],
        max_notional: float = 1e6,
        max_leverage: float = 3.0,
    ) -> Dict[str, str]:
        total_notional = sum(abs(i["qty"] * i["price"]) for i in intents)
        if total_notional > max_notional:
            return {"status": "REJECTED", "reason": "size"}

        var = await workflow.execute_activity(
            value_at_risk,
            total_notional,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        if var > max_notional * 0.2:
            return {"status": "REJECTED", "reason": "risk"}

        return {"status": "APPROVED"}

