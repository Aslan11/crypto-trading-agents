"""Temporal workflow implementations backing the agents."""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Dict, List, Tuple

import asyncio
import json
import os

import aiohttp
import openai
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from temporalio import workflow

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

SYSTEM_PROMPT = (
    "You are a trading ensemble agent. Use the available tools to fetch data, "
    "check risk and place orders as needed. Decide whether to BUY, SELL or HOLD "
    "each symbol when prompted."
)


@workflow.defn
class FeatureStoreWorkflow:
    """Persist feature vectors signalled from agents."""

    def __init__(self) -> None:
        self.store: Dict[Tuple[str, int], Dict] = {}
        self.count = 0

    @workflow.signal
    def add_vector(self, symbol: str, ts: int, data: Dict) -> None:
        self.store[(symbol, ts)] = data
        self.count += 1

    @workflow.query
    def latest_vector(self, symbol: str) -> Dict | None:
        matches = [(ts, vec) for (sym, ts), vec in self.store.items() if sym == symbol]
        if not matches:
            return None
        ts, vec = max(matches, key=lambda kv: kv[0])
        return vec

    @workflow.query
    def next_vector(self, symbol: str, after_ts: int) -> Tuple[int, Dict] | None:
        items = sorted(
            [(ts, v) for (sym, ts), v in self.store.items() if sym == symbol and ts > after_ts],
            key=lambda kv: kv[0],
        )
        if not items:
            return None
        ts, vec = items[0]
        return ts, vec

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


@workflow.defn
class EnsembleWorkflow:
    """LLM-driven trading agent awakened on a schedule."""

    def __init__(self) -> None:
        self.active_symbols: Dict[str, int] = {}
        self.last_price: Dict[str, float] = {}
        self._nudge = asyncio.Event()
        self.conversation: List[Dict] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    @workflow.signal
    def update_price(self, symbol: str, price: float, ts: int) -> None:
        self.active_symbols[symbol] = ts
        self.last_price[symbol] = price

    @workflow.signal
    def nudge(self) -> None:
        self._nudge.set()

    @workflow.run
    async def run(self) -> None:
        base_url = os.environ.get("MCP_SERVER", "http://localhost:8080").rstrip("/")
        mcp_url = base_url + "/mcp"
        openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            async with streamablehttp_client(mcp_url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    print(
                        "[EnsembleAgent] Connected to MCP server with tools:",
                        [t.name for t in tools],
                    )
                    while True:
                        await workflow.wait_condition(lambda: self._nudge.is_set())
                        self._nudge.clear()
                        now = int(workflow.now().timestamp())
                        for sym, ts in list(self.active_symbols.items()):
                            if now - ts > 60:
                                self.active_symbols.pop(sym, None)
                                self.last_price.pop(sym, None)
                        for symbol in list(self.active_symbols):
                            user_msg = f"Time to evaluate {symbol}. Decide BUY, SELL or HOLD using tools."
                            self.conversation.append({"role": "user", "content": user_msg})
                            openai_tools = [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": t.name,
                                        "description": t.description,
                                        "parameters": t.inputSchema,
                                    },
                                }
                                for t in tools
                            ]
                            while True:
                                response = openai_client.chat.completions.create(
                                    model=os.environ.get("OPENAI_MODEL", "o4-mini"),
                                    messages=self.conversation,
                                    tools=openai_tools,
                                    tool_choice="auto",
                                )
                                msg = response.choices[0].message
                                if getattr(msg, "tool_calls", None):
                                    self.conversation.append(
                                        {
                                            "role": msg.role,
                                            "content": msg.content,
                                            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                                        }
                                    )
                                    for call in msg.tool_calls:
                                        func_name = call.function.name
                                        func_args = json.loads(call.function.arguments or "{}")
                                        if func_name in {"pre_trade_risk_check", "place_mock_order"}:
                                            intent = func_args.get("intent", {})
                                            intent.setdefault("symbol", symbol)
                                            intent.setdefault("qty", 1)
                                            intent.setdefault("price", self.last_price.get(symbol, 0.0))
                                            intent_side = intent.get("side") or func_args.get("side")
                                            if intent_side:
                                                intent["side"] = intent_side
                                            func_args["intent"] = intent
                                            if func_name == "pre_trade_risk_check":
                                                func_args.setdefault(
                                                    "intent_id",
                                                    f"{intent.get('side','BUY')}-{symbol}-{now}",
                                                )
                                                func_args.setdefault("intents", [intent])
                                        print(
                                            f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                                        )
                                        result = await session.call_tool(func_name, func_args)
                                        self.conversation.append(
                                            {
                                                "role": "tool",
                                                "tool_call_id": call.id,
                                                "name": func_name,
                                                "content": json.dumps(result.model_dump()),
                                            }
                                        )
                                    continue
                                if getattr(msg, "function_call", None):
                                    self.conversation.append(
                                        {
                                            "role": msg.role,
                                            "content": msg.content,
                                            "function_call": msg.function_call.model_dump(),
                                        }
                                    )
                                    func_name = msg.function_call.name
                                    func_args = json.loads(msg.function_call.arguments or "{}")
                                    if func_name in {"pre_trade_risk_check", "place_mock_order"}:
                                        intent = func_args.get("intent", {})
                                        intent.setdefault("symbol", symbol)
                                        intent.setdefault("qty", 1)
                                        intent.setdefault("price", self.last_price.get(symbol, 0.0))
                                        side = intent.get("side") or func_args.get("side")
                                        if side:
                                            intent["side"] = side
                                        func_args["intent"] = intent
                                        if func_name == "pre_trade_risk_check":
                                            func_args.setdefault(
                                                "intent_id",
                                                f"{intent.get('side','BUY')}-{symbol}-{now}",
                                            )
                                            func_args.setdefault("intents", [intent])
                                    print(
                                        f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                                    )
                                    result = await session.call_tool(func_name, func_args)
                                    self.conversation.append(
                                        {
                                            "role": "function",
                                            "name": func_name,
                                            "content": json.dumps(result.model_dump()),
                                        }
                                    )
                                    continue
                                assistant_reply = msg.content or ""
                                self.conversation.append({"role": "assistant", "content": assistant_reply})
                                print(f"{PINK}[EnsembleAgent] Decision: {assistant_reply}{RESET}")
                                self.conversation = [
                                    {"role": "system", "content": SYSTEM_PROMPT}
                                ]
                                break


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
            now = int(workflow.now().timestamp())
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
