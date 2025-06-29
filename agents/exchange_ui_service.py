"""Simple terminal UI for streaming crypto prices."""

from __future__ import annotations

import curses
import time
import signal
from collections import deque
from typing import Dict, Deque, List

import ccxt


PAIRS: List[str] = ["BTC/USD", "ETH/USD"]
MENU: List[str] = ["ALL"] + PAIRS
FETCH_INTERVAL = float(1)
MAX_POINTS = 60


def _fetch_prices(client: ccxt.coinbaseexchange, prices: Dict[str, Deque[float]]) -> None:
    for pair in PAIRS:
        try:
            ticker = client.fetch_ticker(pair)
            prices[pair].append(ticker["last"])
        except Exception:
            # Ignore network errors and keep existing data
            pass


def _sparkline(data: List[float], width: int) -> str:
    chars = "▁▂▃▄▅▆▇█"
    if not data:
        return ""
    mn = min(data)
    mx = max(data)
    if mx == mn:
        return "─" * min(len(data), width)
    scale = (len(chars) - 1) / (mx - mn)
    values = data[-width:]
    return "".join(chars[int((v - mn) * scale)] for v in values)


def _draw(stdscr: curses.window, selection: int, prices: Dict[str, Deque[float]]) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    menu_w = 16
    for i, item in enumerate(MENU):
        attr = curses.A_REVERSE if i == selection else curses.A_NORMAL
        stdscr.addnstr(i, 0, item.ljust(menu_w - 1), menu_w - 1, attr)

    chart_w = max(10, width - menu_w - 1)
    rows = height - 2
    pairs = PAIRS if selection == 0 else [MENU[selection]]
    for idx, pair in enumerate(pairs):
        if idx * 3 + 2 >= rows:
            break
        data = list(prices[pair])
        line = _sparkline(data, chart_w)
        last = data[-1] if data else 0.0
        y = idx * 3
        stdscr.addnstr(y, menu_w, f"{pair} {last:.2f}", chart_w)
        stdscr.addnstr(y + 1, menu_w, line, chart_w)

    stdscr.refresh()


def main(stdscr: curses.window) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    selection = 0
    client = ccxt.coinbaseexchange()
    prices: Dict[str, Deque[float]] = {p: deque(maxlen=MAX_POINTS) for p in PAIRS}
    last_fetch = 0.0

    running = True

    def stop(_sig, _frm) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)

    while running:
        key = stdscr.getch()
        if key == curses.KEY_UP:
            selection = (selection - 1) % len(MENU)
        elif key == curses.KEY_DOWN:
            selection = (selection + 1) % len(MENU)
        elif key in (ord("q"), ord("Q")):
            break

        now = time.time()
        if now - last_fetch >= FETCH_INTERVAL:
            last_fetch = now
            _fetch_prices(client, prices)

        _draw(stdscr, selection, prices)
        time.sleep(0.1)


if __name__ == "__main__":
    curses.wrapper(main)
