#!/usr/bin/env python
"""Interactive terminal ticker UI service."""

from __future__ import annotations

import argparse
import curses
import json
import logging
import queue
import signal
import threading
import time
import asyncio
from collections import deque
from typing import Deque, Dict, List

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

BRAILLE_DOTS = [
    [0x01, 0x08],  # row 0: dots 1 and 4
    [0x02, 0x10],  # row 1: dots 2 and 5
    [0x04, 0x20],  # row 2: dots 3 and 6
    [0x40, 0x80],  # row 3: dots 7 and 8
]

# layout constants
BORDER = 1
# internal padding inside bordered regions
PADDING = 1
LABEL_WIDTH = 14
LABEL_PAD = 3
X_AXIS_HEIGHT = 1
BORDER_COLOR = 4


class TabBar:
    def __init__(self) -> None:
        self.tabs: List[str] = []
        self.selected = 0

    def update(self, names: List[str]) -> None:
        self.tabs = list(names)
        self.selected = 0

    def handle_key(self, key: int) -> None:
        if not self.tabs:
            return
        if key in (curses.KEY_LEFT, ord("h")):
            self.selected = (self.selected - 1) % len(self.tabs)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self.selected = (self.selected + 1) % len(self.tabs)

    def draw(self, win: "curses._CursesWindow") -> None:
        h, w = win.getmaxyx()
        win.erase()
        win.attron(curses.color_pair(BORDER_COLOR))
        win.box()
        win.attroff(curses.color_pair(BORDER_COLOR))
        y = BORDER + PADDING
        x = BORDER + PADDING
        for idx, name in enumerate(self.tabs):
            if x + len(name) + 2 >= w - BORDER - PADDING:
                break
            if idx == self.selected:
                win.attron(curses.A_REVERSE)
            win.addstr(y, x, f" {name} ")
            if idx == self.selected:
                win.attroff(curses.A_REVERSE)
            x += len(name) + 3


class BrailleChart:
    def __init__(self) -> None:
        pass

    @staticmethod
    def _set_pixel(bits, colors, x: int, y: int, color: int) -> None:
        cy = y // 4
        cx = x // 2
        if cy < 0 or cx < 0:
            return
        bits[cy][cx] |= BRAILLE_DOTS[y % 4][x % 2]
        colors[cy][cx] = color

    def _plot_line(
        self, bits, colors, x0: int, y0: int, x1: int, y1: int, color: int
    ) -> None:
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            self._set_pixel(bits, colors, x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def draw(self, win: "curses._CursesWindow", data: Deque[Tuple[int, float]]) -> None:
        h, w = win.getmaxyx()
        win.erase()
        win.attron(curses.color_pair(BORDER_COLOR))
        win.box()
        win.attroff(curses.color_pair(BORDER_COLOR))

        inner_x = BORDER + PADDING
        inner_y = BORDER + PADDING
        inner_w = max(1, w - 2 * (BORDER + PADDING))
        inner_h = max(1, h - 2 * (BORDER + PADDING))

        chart_w = max(1, inner_w - LABEL_WIDTH - LABEL_PAD)
        chart_h = max(1, inner_h - X_AXIS_HEIGHT)

        px_w = chart_w * 2
        px_h = chart_h * 4
        bits = [[0 for _ in range(chart_w)] for _ in range(chart_h)]
        colors = [[1 for _ in range(chart_w)] for _ in range(chart_h)]

        prices = [p for _, p in data]
        times = [ts for ts, _ in data]
        if not prices:
            win.refresh()
            return

        vals = prices[-px_w:]
        ts_vals = times[-px_w:]
        min_p = min(vals)
        max_p = max(vals)
        if min_p == max_p:
            min_p -= 1
            max_p += 1
        scale_y = (px_h - 1) / (max_p - min_p)

        def y_for(val: float) -> int:
            return int(round((max_p - val) * scale_y))

        prev_x = 0
        prev_y = y_for(vals[0])
        for i in range(1, len(vals)):
            x = i
            y = y_for(vals[i])
            if vals[i] > vals[i - 1]:
                color = 2
            elif vals[i] < vals[i - 1]:
                color = 3
            else:
                color = 1
            self._plot_line(bits, colors, prev_x, prev_y, x, y, color)
            prev_x, prev_y = x, y

        mid_p = (max_p + min_p) / 2
        for cy in range(chart_h):
            label = " " * LABEL_WIDTH
            if cy == 0:
                label = f"High {max_p:.2f}"
            elif cy == chart_h // 2:
                label = f"Avg  {mid_p:.2f}"
            elif cy == chart_h - 1:
                label = f"Low  {min_p:.2f}"
            label = label.rjust(LABEL_WIDTH)
            win.addstr(inner_y + cy, inner_x, label)
            for cx in range(chart_w):
                ch = chr(0x2800 + bits[cy][cx])
                win.addstr(
                    inner_y + cy,
                    inner_x + LABEL_WIDTH + LABEL_PAD + cx,
                    ch,
                    curses.color_pair(colors[cy][cx]),
                )

        # x axis labels every 5 minutes
        if ts_vals:
            next_mark = ((ts_vals[0] // 300) + 1) * 300
            for i, ts in enumerate(ts_vals):
                while ts >= next_mark:
                    pos = i // 2
                    if pos < chart_w:
                        label = time.strftime("%H:%M", time.localtime(next_mark))
                        lx = inner_x + LABEL_WIDTH + LABEL_PAD + pos - len(label) // 2
                        if (
                            lx >= inner_x + LABEL_WIDTH + LABEL_PAD
                            and lx + len(label)
                            < inner_x + LABEL_WIDTH + LABEL_PAD + chart_w
                        ):
                            win.addstr(inner_y + chart_h, lx, label)
                    next_mark += 300

        win.refresh()


def _sse_listener(url: str, q: "queue.Queue[dict]", stop: threading.Event) -> None:
    headers = {"Accept": "text/event-stream"}
    while not stop.is_set():
        try:
            with requests.get(url, stream=True, timeout=60, headers=headers) as resp:
                if resp.status_code != 200:
                    time.sleep(1)
                    continue
                for line in resp.iter_lines():
                    if stop.is_set():
                        break
                    if not line:
                        continue
                    text = line.decode().strip()
                    if not text.startswith("data:"):
                        continue
                    try:
                        evt = json.loads(text[5:])
                    except Exception:
                        continue
                    q.put(evt)
        except Exception as err:
            logger.warning("SSE listener error: %s", err)
            time.sleep(1)


def _portfolio_poller(url: str, q: "queue.Queue[dict]", stop: threading.Event) -> None:
    """Periodically fetch portfolio status and push value events."""

    async def _run() -> None:
        while not stop.is_set():
            try:
                async with streamablehttp_client(url) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        while not stop.is_set():
                            try:
                                result = await session.call_tool(
                                    "get_portfolio_status", {}
                                )
                                # fastmcp's call_tool returns a dataclass with
                                # `data` and `structured_content` attributes.
                                if hasattr(result, "data") and result.data is not None:
                                    data = result.data
                                elif (
                                    hasattr(result, "structured_content")
                                    and result.structured_content is not None
                                ):
                                    data = result.structured_content
                                elif (
                                    hasattr(result, "structuredContent")
                                    and result.structuredContent is not None
                                ):
                                    data = result.structuredContent
                                else:
                                    data = {}
                                q.put(
                                    {
                                        "type": "portfolio_status",
                                        "ts": int(time.time()),
                                        "data": data,
                                    }
                                )
                            except Exception as err:
                                logger.warning("Portfolio status poll error: %s", err)
                            await asyncio.sleep(1)
            except Exception as err:
                logger.warning("Portfolio poller connection error: %s", err)
                await asyncio.sleep(1)

    asyncio.run(_run())


def run_curses(
    stdscr: "curses._CursesWindow", q: "queue.Queue[dict]", stop: threading.Event
) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    # approximate orange if extended colors are available
    orange = 208 if curses.COLORS >= 16 else curses.COLOR_YELLOW
    try:
        if curses.can_change_color() and curses.COLORS >= 16:
            curses.init_color(orange, 1000, 647, 0)
    except Exception:
        pass
    curses.init_pair(4, orange, -1)
    stdscr.nodelay(True)
    stdscr.timeout(100)

    tabbar = TabBar()
    data: Dict[str, Deque[tuple[int, float]]] = {}
    latest_price: Dict[str, float] = {}
    portfolio_history: Deque[tuple[int, float]] = deque(maxlen=360)
    tabbar.update(["Portfolio"])
    data["Portfolio"] = portfolio_history
    chart = BrailleChart()
    last_draw = 0.0

    while not stop.is_set():
        # handle events
        try:
            while True:
                evt = q.get_nowait()
                if evt.get("type") == "pairs":
                    symbols = evt.get("symbols", [])
                    tabbar.update(["Portfolio"] + symbols)
                    for s in symbols:
                        data.setdefault(s, deque(maxlen=360))
                    data.setdefault("Portfolio", portfolio_history)
                elif evt.get("type") == "portfolio_status":
                    data_dict = evt.get("data", {})
                    cash = data_dict.get("cash", 0.0)
                    positions = data_dict.get("positions", {})
                    entry_prices = data_dict.get("entry_prices", {})
                    pnl = data_dict.get("pnl", 0.0)
                    ts = evt.get("ts", int(time.time()))
                    entry_value = sum(
                        float(qty) * float(entry_prices.get(sym, 0.0))
                        for sym, qty in positions.items()
                    )
                    value = cash + entry_value + float(pnl)
                    portfolio_history.append((ts, float(value)))
                elif evt.get("type") == "portfolio_value":
                    ts = evt.get("ts", int(time.time()))
                    value = evt.get("value")
                    if value is not None:
                        portfolio_history.append((ts, float(value)))
                else:
                    sym = evt.get("symbol")
                    if not sym:
                        continue
                    if sym not in tabbar.tabs:
                        tabbar.tabs.append(sym)
                    price = evt.get("price")
                    if price is None:
                        d = evt.get("data", {})
                        if "last" in d:
                            price = d["last"]
                        elif {"bid", "ask"}.issubset(d):
                            price = (d["bid"] + d["ask"]) / 2
                    ts = evt.get("ts")
                    if ts is None:
                        ts_ms = evt.get("data", {}).get("timestamp")
                        ts = (
                            int(ts_ms / 1000) if ts_ms is not None else int(time.time())
                        )
                    if price is not None:
                        latest_price[sym] = float(price)
                        data.setdefault(sym, deque(maxlen=360)).append(
                            (ts, float(price))
                        )
        except queue.Empty:
            pass

        key = stdscr.getch()
        if key in (ord("q"), 27):
            stop.set()
            break
        tabbar.handle_key(key)

        now = time.time()
        if now - last_draw >= 1:
            stdscr.erase()
            total_h, total_w = stdscr.getmaxyx()
            tab_h = 1 + 2 * PADDING + 2 * BORDER
            tab_win = stdscr.derwin(tab_h, total_w, 0, 0)
            tabbar.draw(tab_win)
            if not tabbar.tabs:
                msg = "Waiting for tickers..."
                stdscr.addstr(total_h // 2, max(0, (total_w - len(msg)) // 2), msg)
                stdscr.refresh()
            else:
                symbol = tabbar.tabs[tabbar.selected]
                chart_h = total_h - tab_h
                chart_win = stdscr.derwin(chart_h, total_w, tab_h, 0)
                chart.draw(chart_win, data.get(symbol, deque()))
                stdscr.refresh()
            last_draw = now


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://localhost:8080/signal/market_tick",
        help="SSE stream URL",
    )
    parser.add_argument(
        "--status-url",
        default="http://localhost:8080/mcp/",
        help="MCP base URL for portfolio status",
    )
    args = parser.parse_args()

    q: queue.Queue[dict] = queue.Queue()
    stop = threading.Event()

    t = threading.Thread(target=_sse_listener, args=(args.url, q, stop), daemon=True)
    p = threading.Thread(
        target=_portfolio_poller, args=(args.status_url, q, stop), daemon=True
    )
    t.start()
    p.start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    curses.wrapper(run_curses, q, stop)
    stop.set()
    t.join()
    p.join()


if __name__ == "__main__":
    main()
