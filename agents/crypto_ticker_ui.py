import asyncio
import json
import os
import signal
from typing import Dict, Any

import aiohttp
from rich.console import Console
from rich.table import Table
from rich.live import Live

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
REFRESH_SEC = float(os.environ.get("TICKER_REFRESH", "1"))

# Symbol -> latest vector data
LATEST: Dict[str, Dict[str, Any]] = {}
STOP = asyncio.Event()


def _handle_sigint() -> None:
    STOP.set()


def build_table() -> Table:
    table = Table(title="Crypto Tickrs")
    table.add_column("Pair")
    table.add_column("Price", justify="right")
    table.add_column("1m %", justify="right")
    if not LATEST:
        table.add_row("[italic]Waiting for pairs...[/]", "", "")
        return table
    for sym in sorted(LATEST):
        data = LATEST[sym]
        price = data.get("mid")
        ret1m = data.get("ret1m")
        price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "-"
        if isinstance(ret1m, (int, float)):
            color = "green" if ret1m >= 0 else "red"
            ret_str = f"[{color}]{ret1m:+.2f}%[/]"
        else:
            ret_str = "-"
        table.add_row(sym, price_str, ret_str)
    return table


async def watch_vectors() -> None:
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
    headers = {"Accept": "text/event-stream"}
    cursor = 0
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not STOP.is_set():
            try:
                async with session.get(url, params={"after": cursor}, headers=headers) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(1)
                        continue
                    while not STOP.is_set():
                        line = await resp.content.readline()
                        if not line:
                            break
                        text = line.decode().strip()
                        if not text.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(text[5:].strip())
                        except Exception:
                            continue
                        sym = evt.get("symbol")
                        ts = evt.get("ts")
                        data = evt.get("data")
                        if sym and isinstance(data, dict):
                            LATEST[sym] = data
                        if isinstance(ts, int):
                            cursor = max(cursor, ts)
            except Exception:
                await asyncio.sleep(1)


async def run_ui() -> None:
    console = Console()
    with Live(build_table(), console=console, refresh_per_second=4) as live:
        while not STOP.is_set():
            live.update(build_table())
            await asyncio.sleep(REFRESH_SEC)


async def main() -> None:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _handle_sigint)
    watcher = asyncio.create_task(watch_vectors())
    try:
        await run_ui()
    finally:
        STOP.set()
        await watcher


if __name__ == "__main__":
    asyncio.run(main())
