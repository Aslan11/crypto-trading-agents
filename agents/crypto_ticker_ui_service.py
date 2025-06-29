import asyncio
import os
import signal
from typing import Dict

import aiohttp
from rich.live import Live
from rich.table import Table

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

STOP_EVENT = asyncio.Event()
PRICES: Dict[str, float] = {}


def _render_table() -> Table:
    table = Table(title="Crypto Prices", box=None)
    table.add_column("Symbol", style="cyan")
    table.add_column("Last Price", justify="right")
    for sym, price in sorted(PRICES.items()):
        price_str = f"{price:,.2f}" if price is not None else "-"
        table.add_row(sym, price_str)
    return table


async def _poll_ticks(session: aiohttp.ClientSession) -> None:
    cursor = 0
    backoff = 1
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/market_tick"
    while not STOP_EVENT.is_set():
        try:
            async with session.get(url, params={"after": cursor}) as resp:
                if resp.status == 200:
                    events = await resp.json()
                    backoff = 1
                else:
                    events = []
        except Exception:
            events = []
        if not events:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        for evt in events:
            sym = evt.get("symbol")
            ts = evt.get("ts")
            data = evt.get("data") or {}
            price = data.get("last")
            if sym and price is not None:
                PRICES[sym] = price
            if ts is not None:
                cursor = max(cursor, ts)
        await asyncio.sleep(0.1)


async def main() -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, STOP_EVENT.set)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        ticker_task = asyncio.create_task(_poll_ticks(session))
        with Live(_render_table(), refresh_per_second=2) as live:
            while not STOP_EVENT.is_set():
                live.update(_render_table())
                await asyncio.sleep(1)
        ticker_task.cancel()
        await asyncio.gather(ticker_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
