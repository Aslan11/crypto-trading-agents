"""Ensemble workflow for storing latest prices and approved intents."""

from __future__ import annotations

from typing import Dict, List
from temporalio import workflow


@workflow.defn
class EnsembleWorkflow:
    """Store latest prices and approved intents."""

    def __init__(self) -> None:
        self.latest_price: Dict[str, float] = {}
        self.intents: List[Dict] = []

    @workflow.signal
    def update_price(self, symbol: str, price: float) -> None:
        self.latest_price[symbol] = price

    @workflow.signal
    def record_intent(self, intent: Dict) -> None:
        self.intents.append(intent)

    @workflow.query
    def get_price(self, symbol: str) -> float | None:
        return self.latest_price.get(symbol)

    @workflow.query
    def get_intents(self) -> List[Dict]:
        return list(self.intents)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)