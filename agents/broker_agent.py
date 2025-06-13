from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import List, Dict

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from agents.workflows import ExecutionLedgerWorkflow
from agents.utils import print_banner
from agents.shared_bus import enqueue_intent

import aiohttp

try:
    import openai
except Exception:  # pragma: no cover - optional dependency
    openai = None


MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
EXCHANGE = os.environ.get("EXCHANGE", "coinbaseexchange")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

LEDGER_WF_ID = "mock-ledger"
TEMPORAL_CLIENT: Client | None = None


def _parse_symbols(text: str) -> List[str]:
    """Return list of crypto symbols in ``text``."""
    return re.findall(r"[A-Z0-9]+/[A-Z0-9]+", text.upper())


async def _filter_available_symbols(exchange: str, symbols: List[str]) -> List[str]:
    """Return ``symbols`` that are tradable on ``exchange``."""
    import ccxt.async_support as ccxt

    exchange_cls = getattr(ccxt, exchange)
    client = exchange_cls()
    try:
        await client.load_markets()
        available = set(client.symbols)
        return [sym for sym in symbols if sym in available]
    finally:
        await client.close()


async def _get_client() -> Client:
    """Return connected Temporal client."""
    global TEMPORAL_CLIENT
    if TEMPORAL_CLIENT is None:
        address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
        TEMPORAL_CLIENT = await Client.connect(address, namespace=namespace)
    return TEMPORAL_CLIENT


async def _ensure_ledger(client: Client) -> None:
    """Ensure the ledger workflow is running."""
    handle = client.get_workflow_handle(LEDGER_WF_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                ExecutionLedgerWorkflow.run,
                id=LEDGER_WF_ID,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
            )
        else:
            raise


async def _get_status() -> Dict[str, object]:
    """Return current cash, positions and PnL from the ledger."""
    client = await _get_client()
    await _ensure_ledger(client)
    handle = client.get_workflow_handle(LEDGER_WF_ID)
    cash = await handle.query("get_cash")
    pnl = await handle.query("get_pnl")
    try:
        positions = await handle.query("get_positions")
        entry_prices = await handle.query("get_entry_prices")
    except Exception:  # pragma: no cover - older workflow version
        positions = {}
        entry_prices = {}
    return {"cash": cash, "pnl": pnl, "positions": positions, "entry_prices": entry_prices}


async def _format_status(status: Dict[str, object]) -> str:
    """Return a friendly summary of the ledger status using the LLM if available."""
    cash = status.get("cash", 0.0)
    pnl = status.get("pnl", 0.0)
    positions = status.get("positions", {})
    entry_prices = status.get("entry_prices", {})

    default = (
        f"Cash: ${cash:.2f}\n"
        f"Holdings: {positions}\n"
        f"Entry Prices: {entry_prices}\n"
        f"Gains/Losses: ${pnl:.2f}"
    )

    if openai is None:
        return default

    client = openai.AsyncOpenAI()
    prompt = (
        "Summarize the following portfolio status for a human audience. "
        "Format the output for display in a terminal using short lines.\n"
        f"{status}"
    )
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Your responses will be printed to a terminal. Use concise wording and line breaks for readability.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:  # pragma: no cover - network issues
        logger.error("LLM call failed: %s", exc)
        return default


async def _print_status() -> None:
    status = await _get_status()
    summary = await _format_status(status)
    print(summary)


async def _prompt_user() -> tuple[List[str], list[dict]]:
    """Prompt the user for pairs or accept LLM suggestions."""
    if openai is None:
        print("openai package not installed")
        user_msg = input("Pairs to trade (e.g. BTC/USD,ETH/USD): ")
        return _parse_symbols(user_msg), []

    client = openai.AsyncOpenAI()
    messages = [
        {
            "role": "system",
            "content": (
                "You are a crypto trading broker. "
                "Only discuss cryptocurrency trading and decline other topics."
            ),
        }
    ]

    suggest_q = (
        "Which cryptocurrency pairs would you recommend for a momentum "
        "trading strategy on Coinbase Exchange? Only include pairs that "
        "actually trade on Coinbase. Respond with a comma separated list "
        "like BTC/USD,ETH/USD."
    )
    messages.append({"role": "user", "content": suggest_q})
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
        )
        suggest_reply = resp.choices[0].message.content.strip()
    except Exception as exc:  # pragma: no cover - network issues
        logger.error("LLM call failed: %s", exc)
        suggest_reply = ""
    print(suggest_reply)
    messages.append({"role": "assistant", "content": suggest_reply})
    suggested = _parse_symbols(suggest_reply)
    if suggested:
        suggested = await _filter_available_symbols(EXCHANGE, suggested)

    prompt = (
        "Press Enter to accept these pairs or provide your own "
        "(e.g. BTC/USD,ETH/USD)"
    )
    print(prompt)
    user_input = input("> ").strip()

    if not user_input:
        symbols = suggested
        user_msg = "I'll trade: " + ",".join(symbols)
    else:
        symbols = _parse_symbols(user_input)
        user_msg = user_input

    if symbols:
        filtered = await _filter_available_symbols(EXCHANGE, symbols)
        missing = set(symbols) - set(filtered)
        symbols = filtered
        if missing:
            print(f"Ignoring unavailable symbols: {', '.join(sorted(missing))}")
        if not user_input:
            user_msg = "I'll trade: " + ",".join(symbols)

    messages.append({"role": "user", "content": user_msg})
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as exc:  # pragma: no cover - network issues
        logger.error("LLM call failed: %s", exc)
        reply = ""
    print(reply)
    messages.append({"role": "assistant", "content": reply})

    if not symbols:
        print("No valid crypto symbols found. Exiting.")
    return symbols, messages


async def _start_stream(symbols: List[str]) -> None:
    if not symbols:
        print("No valid symbols to stream.")
        return

    payload = {"exchange": EXCHANGE, "symbols": symbols}
    url = f"http://{MCP_HOST}:{MCP_PORT}/tools/SubscribeCEXStream"
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 202:
                    data = await resp.json()
                    print(
                        f"Started workflow {data['workflow_id']} run {data['run_id']}"
                    )
                else:
                    print(f"Failed to start workflow: {resp.status}")
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("HTTP request failed: %s", exc)


def _parse_order_simple(text: str) -> dict | None:
    """Quickly parse ``text`` for ``BUY``/``SELL`` orders."""
    parts = text.split()
    if len(parts) not in {3, 4}:
        return None
    side = parts[0].upper()
    if side not in {"BUY", "SELL"}:
        return None
    symbol = parts[1].upper()
    try:
        qty = float(parts[2])
    except ValueError:
        return None
    price: float | None = None
    if len(parts) == 4:
        try:
            price = float(parts[3])
        except ValueError:
            return None
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "ts": int(datetime.utcnow().timestamp()),
    }


async def _parse_order(text: str) -> dict | None:
    """Parse a natural language order using the LLM if available."""
    intent = _parse_order_simple(text)
    if intent or openai is None:
        return intent

    prompt = (
        "Extract a cryptocurrency order from the text. "
        "Return ONLY JSON with keys: side, symbol, qty, price."
    )

    try:
        client_ai = openai.AsyncOpenAI()
        resp = await client_ai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content.strip())
        side = data.get("side", "").upper()
        symbol = data.get("symbol", "").upper()
        qty = float(data.get("qty"))
        price = float(data.get("price"))
        if side in {"BUY", "SELL"} and symbol:
            return {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "ts": int(datetime.utcnow().timestamp()),
            }
    except Exception as exc:  # pragma: no cover - network or parse errors
        logger.error("Order parsing failed: %s", exc)
    return None


async def _chat_loop(messages: list[dict]) -> None:
    """Interactive chat loop for the broker."""
    if openai is None:
        return

    client = openai.AsyncOpenAI()
    print(
        "Type 'status' to view portfolio, 'add <SYM>' to start a new stream,\n"
        "or simply tell me in plain language when you want to buy or sell.\n"
        "Type 'quit' to exit."
    )
    while True:
        user_msg = input("> ").strip()
        if not user_msg:
            continue
        if user_msg.lower() in {"quit", "exit"}:
            break
        if user_msg.lower().startswith("status"):
            await _print_status()
            continue
        if any(w in user_msg.lower() for w in {"add", "track", "subscribe"}):
            symbols = _parse_symbols(user_msg)
            if symbols:
                symbols = await _filter_available_symbols(EXCHANGE, symbols)
                if not symbols:
                    print("No valid symbols found.")
                else:
                    await _start_stream(symbols)
                continue
        order = await _parse_order(user_msg)
        if order:
            enqueue_intent(order)
            if order["price"] is None:
                print(
                    f"Enqueued {order['side']} {order['qty']} {order['symbol']} @ market"
                )
            else:
                print(
                    f"Enqueued {order['side']} {order['qty']} {order['symbol']} @ {order['price']}"
                )
            continue
        messages.append({"role": "user", "content": user_msg})
        try:
            resp = await client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
            )
            reply = resp.choices[0].message.content.strip()
        except Exception as exc:  # pragma: no cover - network issues
            logger.error("LLM call failed: %s", exc)
            reply = ""
        print(reply)
        messages.append({"role": "assistant", "content": reply})


async def main() -> None:
    print_banner(
        "Broker Agent",
        "Interactively start market data streams",
    )

    symbols, messages = await _prompt_user()
    if not symbols:
        return
    await _start_stream(symbols)
    await _chat_loop(messages)


if __name__ == "__main__":
    asyncio.run(main())
