from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime
from decimal import Decimal

import aiohttp

try:
    from agents.shared_bus import APPROVED_INTENT_QUEUE
except Exception:  # pragma: no cover - fallback for missing module
    APPROVED_INTENT_QUEUE: asyncio.Queue[dict] = asyncio.Queue()


MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

cash = Decimal("20")
positions: dict[str, Decimal] = {}
_last_price: dict[str, Decimal] = {}
FILL_COUNT = 0
STOP_EVENT = asyncio.Event()


def _update_ledger(fill: dict) -> None:
    global cash, FILL_COUNT
    side = fill["side"]
    symbol = fill["symbol"]
    qty = Decimal(str(fill["qty"]))
    price = Decimal(str(fill["fill_price"]))
    cost = Decimal(str(fill["cost"]))

    _last_price[symbol] = price

    if side == "BUY":
        cash -= cost
        positions[symbol] = positions.get(symbol, Decimal("0")) + qty
    else:
        cash += cost
        positions[symbol] = positions.get(symbol, Decimal("0")) - qty

    FILL_COUNT += 1

    pos_qty = positions.get(symbol, Decimal("0"))
    ts = datetime.utcfromtimestamp(fill["ts"]).strftime("%Y-%m-%d %H:%M")
    logger.info(
        "[%s] %s %.6f %s @%s  cost=$%.2f  cash=$%.2f  pos=%.6f",
        ts,
        side,
        qty,
        symbol,
        price,
        cost,
        cash,
        pos_qty,
    )

    if FILL_COUNT % 10 == 0:
        pnl = cash + sum(q * _last_price.get(sym, Decimal("0")) for sym, q in positions.items())
        logger.info("Total P&L: $%.2f", pnl)


async def _place_order(session: aiohttp.ClientSession, intent: dict) -> None:
    qty = Decimal(str(intent["qty"]))
    price = Decimal(str(intent["price"]))
    cost = qty * price

    if intent["side"] == "BUY" and cash < cost:
        logger.warning("INSUFFICIENT_FUNDS for %s", intent)
        return

    try:
        resp = await session.post(
            f"http://{MCP_HOST}:{MCP_PORT}/tools/PlaceMockOrder",
            json={"intent": intent},
        )
        data = await resp.json()
        wf_id = data["workflow_id"]
        run_id = data["run_id"]
    except Exception as exc:  # pragma: no cover - network errors
        logger.error("Failed to start workflow: %s", exc)
        return

    fill = None
    while not STOP_EVENT.is_set():
        try:
            status_resp = await session.get(
                f"http://{MCP_HOST}:{MCP_PORT}/workflow/{wf_id}/{run_id}"
            )
            payload = await status_resp.json()
            status = payload.get("status")
            if status == "COMPLETED":
                fill = payload.get("result")
                break
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("Status poll failed: %s", exc)
            await asyncio.sleep(0.5)
            continue
        await asyncio.sleep(0.5)

    if fill:
        _update_ledger(fill)


async def _run() -> None:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not STOP_EVENT.is_set():
            intent = await APPROVED_INTENT_QUEUE.get()
            await _place_order(session, intent)


async def main() -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, STOP_EVENT.set)
    try:
        await _run()
    finally:
        pnl = cash + sum(q * _last_price.get(sym, Decimal("0")) for sym, q in positions.items())
        logger.info("Final cash=$%.2f positions=%s P&L=$%.2f", cash, positions, pnl)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - shutdown
        pass

