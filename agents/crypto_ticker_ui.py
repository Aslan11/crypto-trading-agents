import asyncio
import json
import os
import logging
from typing import Dict, List
import re
import contextlib

import aiohttp
from textual.app import App, ComposeResult
from textual.widgets import TabbedContent, TabPane, Tabs, Static
from textual.css.query import NoMatches

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

REFRESH_SEC = float(os.environ.get("TICKER_REFRESH", "1"))
MAX_CANDLES = int(os.environ.get("TICKER_WINDOW", "20"))
USE_COLOR = os.environ.get("TICKER_COLOR", "false").lower() not in ("0", "no", "false")


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=LOG_LEVEL,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    filename=os.environ.get("TICKER_LOG", "crypto_ticker_ui.log"),
)
logger = logging.getLogger(__name__)


class TickerApp(App):
    """A simple Textual app displaying a ticker per symbol in tabs."""

    BINDINGS = [
        ("left", "previous_tab", "Prev"),
        ("right", "next_tab", "Next"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.data: Dict[str, List[float]] = {}
        self.symbol_map: Dict[str, str] = {}
        self.cursor = 0
        self.watcher: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield TabbedContent()

    @staticmethod
    def _pane_id(sym: str) -> str:
        """Return a safe DOM id derived from ``sym``."""
        safe = re.sub(r"[^0-9A-Za-z_-]", "-", sym)
        if safe and safe[0].isdigit():
            safe = f"sym-{safe}"
        return safe

    async def on_mount(self) -> None:
        self.tabbed = self.query_one(TabbedContent)
        await self.tabbed.add_pane(
            TabPane("Waiting", Static("Waiting for pairs…", expand=True), id="__wait")
        )
        self.tabbed.active = "__wait"
        logger.info("Ticker UI mounted, awaiting data from %s:%s", MCP_HOST, MCP_PORT)
        self.watcher = asyncio.create_task(self.watch_vectors())
        self.set_interval(REFRESH_SEC, self.update_current_tab)

    async def on_unmount(self) -> None:
        if self.watcher:
            self.watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watcher
        logger.info("Ticker UI shutting down")

    async def watch_vectors(self) -> None:
        """Continuously fetch feature vectors from the MCP server."""

        url = f"http://{MCP_HOST}:{MCP_PORT}/signal/feature_vector"
        headers = {"Accept": "text/event-stream"}
        timeout = aiohttp.ClientTimeout(total=None)
        backoff = 1

        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    logger.info("Connecting to %s", url)
                    async with session.get(
                        url, params={"after": self.cursor}, headers=headers
                    ) as resp:
                        logger.info("SSE connected with status %s", resp.status)
                        if resp.status != 200:
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 30)
                            continue

                        backoff = 1
                        while True:
                            line = await resp.content.readline()
                            if not line:
                                break
                            text = line.decode().strip()
                            if not text or not text.startswith("data:"):
                                continue
                            try:
                                evt = json.loads(text[5:].strip())
                            except Exception:
                                logger.exception("Failed to parse SSE line: %s", text)
                                continue

                            sym = evt.get("symbol")
                            ts = evt.get("ts")
                            data = evt.get("data")
                            if sym and isinstance(data, dict):
                                price = data.get("mid")
                                if isinstance(price, (int, float)):
                                    logger.debug("%s price=%s ts=%s", sym, price, ts)
                                    key = self._pane_id(sym)
                                    self.symbol_map[key] = sym
                                    self.data.setdefault(key, []).append(price)
                                    self.data[key] = self.data[key][-120:]
                                    await self.ensure_tab(sym)
                            if isinstance(ts, int):
                                self.cursor = max(self.cursor, ts)
                except Exception as exc:
                    logger.warning("SSE error: %s", exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)

    async def ensure_tab(self, sym: str) -> None:
        """Create a tab for ``sym`` if it doesn't already exist."""
        pane_id = self._pane_id(sym)

        try:
            self.tabbed.get_pane(pane_id)
        except NoMatches:
            logger.info("Creating tab for %s", sym)
            try:
                self.tabbed.get_pane("__wait")
            except NoMatches:
                pass
            else:
                await self.tabbed.remove_pane("__wait")

            await self.tabbed.add_pane(
                TabPane(sym, Static("Waiting for data…", expand=True), id=pane_id)
            )

            if self.tabbed.active in (None, "__wait"):
                self.tabbed.active = pane_id
        else:
            logger.debug("Tab for %s already exists", sym)

    def update_current_tab(self) -> None:
        pane = self.tabbed.active_pane
        if not pane:
            return
        logger.debug("Updating pane %s", pane.id)
        static = pane.query_one(Static)
        if pane.id and pane.id in self.data:
            width = static.size.width
            height = static.size.height
            static.update(self.render_graph(pane.id, width, height))
        elif pane.id == "__wait":
            static.update("Waiting for pairs…")

    def render_graph(self, sym: str, width: int, height: int) -> str:
        prices = self.data.get(sym, [])
        if len(prices) < 2:
            return "Waiting for data…"

        logger.debug(
            "Rendering graph for %s with %d points (%dx%d)",
            sym,
            len(prices),
            width,
            height,
        )

        n = max(1, min(MAX_CANDLES, (width - 8) // 3))
        values = prices[-(n + 1) :]
        hi = max(values)
        lo = min(values)
        avg = (hi + lo) / 2

        def _row_for(val: float) -> int:
            targets = [hi, avg, lo]
            diffs = [abs(val - t) for t in targets]
            return diffs.index(min(diffs))

        rows = [[" "] * (n * 3) for _ in range(3)]
        for i in range(n):
            open_p = values[i]
            close_p = values[i + 1]
            high_p = max(open_p, close_p)
            low_p = min(open_p, close_p)
            bull = close_p >= open_p
            open_r = _row_for(open_p)
            close_r = _row_for(close_p)
            high_r = _row_for(high_p)
            low_r = _row_for(low_p)
            col = i * 3

            for r in range(min(high_r, low_r), max(high_r, low_r) + 1):
                if r < min(open_r, close_r) or r > max(open_r, close_r):
                    rows[r][col + 1] = "│"

            upper = min(open_r, close_r)
            lower = max(open_r, close_r)
            box_color = ("\033[32m" if bull else "\033[31m") if USE_COLOR else ""
            reset = "\033[0m" if USE_COLOR else ""
            filler = " " if bull else "█"
            if open_r == close_r:
                box = f"{box_color}┌{filler}┐{reset}"
                rows[open_r][col : col + 3] = list(box)
            else:
                rows[upper][col : col + 3] = list(f"{box_color}┌─┐{reset}")
                for r in range(upper + 1, lower):
                    rows[r][col : col + 3] = list(f"{box_color}│{filler}│{reset}")
                rows[lower][col : col + 3] = list(f"{box_color}└─┘{reset}")

        label_width = max(len(f"H {hi:.2f}"), len(f"A {avg:.2f}"), len(f"L {lo:.2f}")) + 2
        labels = [
            f"H {hi:.2f}".ljust(label_width - 1) + "┤",
            f"A {avg:.2f}".ljust(label_width - 1) + "┤",
            f"L {lo:.2f}".ljust(label_width - 1) + "┼",
        ]

        lines = [labels[r] + "".join(rows[r]) for r in range(3)]
        arrow = " " * label_width + "─" * (n * 3) + "►"

        tick_row = [" "] * (n * 3)
        label_row = [" "] * (n * 3)
        for i in range(n):
            c = i * 3 + 1
            tick_row[c] = "^"
            tag = f"t-{n - 1 - i}" if i < n - 1 else "t0"
            start = max(0, c - len(tag) // 2)
            for j, ch in enumerate(tag):
                idx = start + j
                if idx < len(label_row):
                    label_row[idx] = ch

        lines.append(arrow)
        lines.append(" " * label_width + "".join(tick_row))
        lines.append(" " * label_width + "".join(label_row))
        return "\n".join(lines[:height])

    def action_next_tab(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one(Tabs).action_next_tab()

    def action_previous_tab(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one(Tabs).action_previous_tab()

    def action_quit(self) -> None:
        self.exit()


if __name__ == "__main__":
    TickerApp().run()
