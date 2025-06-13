from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from bisect import insort_right, bisect_right
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Tuple

from temporalio import workflow


@dataclass(order=True)
class VectorEntry:
    ts: int
    data: Dict = field(compare=False)


@workflow.defn
class FeatureStoreWorkflow:
    """Persist feature vectors signalled from agents."""

    def __init__(self) -> None:
        self.store: Dict[str, List[VectorEntry]] = {}
        self.count = 0

    @workflow.signal
    def add_vector(self, symbol: str, ts: int, data: Dict) -> None:
        vecs = self.store.setdefault(symbol, [])
        insort_right(vecs, VectorEntry(ts, data))
        self.count += 1

    @workflow.query
    def latest_vector(self, symbol: str) -> Dict | None:
        vecs = self.store.get(symbol)
        if not vecs:
            return None
        return vecs[-1].data

    @workflow.query
    def next_vector(self, symbol: str, after_ts: int) -> Tuple[int, Dict] | None:
        vecs = self.store.get(symbol)
        if not vecs:
            return None
        idx = bisect_right(vecs, VectorEntry(after_ts, {}))
        if idx >= len(vecs):
            return None
        entry = vecs[idx]
        return entry.ts, entry.data

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


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
class MomentumWorkflow:
    """Detect SMA crossovers from feature vectors."""

    def __init__(self) -> None:
        self.vectors: deque[Dict] = deque(maxlen=2)
        self.signals: List[Dict] = []
        self.cooldown = 30
        self.last_sent = 0

    @workflow.signal
    def add_vector(self, vector: Dict) -> None:
        self.vectors.append(vector)

    @workflow.query
    def next_signal(self, after_ts: int) -> Dict | None:
        items = [s for s in self.signals if s["ts"] > after_ts]
        return items[0] if items else None

    @staticmethod
    def _cross(prev: Dict, curr: Dict) -> str | None:
        p1, p5 = prev.get("sma1"), prev.get("sma5")
        c1, c5 = curr.get("sma1"), curr.get("sma5")
        if None in (p1, p5, c1, c5):
            return None
        if p1 < p5 and c1 > c5:
            return "BUY"
        if p1 > p5 and c1 < c5:
            return "SELL"
        return None

    @workflow.run
    async def run(self, cooldown: int = 30) -> None:
        self.cooldown = cooldown
        while True:
            await workflow.wait_condition(lambda: len(self.vectors) == 2)
            prev, curr = self.vectors[0], self.vectors[1]
            side = self._cross(prev, curr)
            if not side:
                self.vectors.popleft()
                continue
            now = int(datetime.utcnow().timestamp())
            if now - self.last_sent < self.cooldown:
                self.vectors.popleft()
                continue
            self.last_sent = now
            self.signals.append({"side": side, "ts": now})
            self.vectors.popleft()


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
