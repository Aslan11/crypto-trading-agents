"""Temporal workflow implementations backing the agents."""

from __future__ import annotations

from decimal import Decimal
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




@workflow.defn
class ExecutionLedgerWorkflow:
    """Maintain mock execution ledger state."""

    def __init__(self) -> None:
        self.initial_cash = Decimal("250000")
        self.cash = Decimal("250000")
        self.positions: Dict[str, Decimal] = {}
        self.last_price: Dict[str, Decimal] = {}
        self.entry_price: Dict[str, Decimal] = {}
        self.fill_count = 0

    @workflow.signal
    def record_fill(self, fill: Dict) -> None:
        side = fill["side"]
        symbol = fill["symbol"]
        qty = Decimal(str(fill["qty"]))
        price = Decimal(str(fill["fill_price"]))
        cost = Decimal(str(fill["cost"]))
        self.last_price[symbol] = price
        current_qty = self.positions.get(symbol, Decimal("0"))
        if side == "BUY":
            self.cash -= cost
            new_qty = current_qty + qty
            avg_price = self.entry_price.get(symbol, Decimal("0"))
            if new_qty > 0:
                self.entry_price[symbol] = (
                    (avg_price * current_qty + price * qty) / new_qty
                )
            self.positions[symbol] = new_qty
        else:
            self.cash += cost
            new_qty = current_qty - qty
            if new_qty <= 0:
                self.positions.pop(symbol, None)
                self.entry_price.pop(symbol, None)
            else:
                self.positions[symbol] = new_qty
        self.fill_count += 1

    @workflow.query
    def get_pnl(self) -> float:
        position_value = sum(
            q * self.last_price.get(sym, Decimal("0"))
            for sym, q in self.positions.items()
        )
        pnl = (self.cash + position_value) - self.initial_cash
        return float(pnl)

    @workflow.query
    def get_cash(self) -> float:
        return float(self.cash)

    @workflow.query
    def get_positions(self) -> Dict[str, float]:
        """Return current position sizes as floats."""
        return {sym: float(q) for sym, q in self.positions.items()}

    @workflow.query
    def get_entry_prices(self) -> Dict[str, float]:
        """Return weighted average entry price for each symbol."""
        return {sym: float(p) for sym, p in self.entry_price.items()}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)
