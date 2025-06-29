import asyncio
import json
import os
import logging
from typing import Dict, List
import re
import contextlib

import aiohttp
import plotille
from textual.app import App, ComposeResult
from textual.widgets import TabbedContent, TabPane, Tabs, Static
from textual.css.query import NoMatches

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

REFRESH_SEC = float(os.environ.get("TICKER_REFRESH", "1"))


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
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
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", sym)
        if re.match(r"^[0-9]", safe):
            safe = f"sym-{safe}"
        return safe

    async def on_mount(self) -> None:
        self.tabbed = self.query_one(TabbedContent)
        await self.tabbed.add_pane(
            TabPane("Waiting", Static("Waiting for pairs…"), id="__wait")
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
                TabPane(sym, Static("Waiting for data…"), id=pane_id)
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
            static.update(self.render_graph(pane.id))
        elif pane.id == "__wait":
            static.update("Waiting for pairs…")

    def render_graph(self, sym: str) -> str:
        prices = self.data.get(sym, [])
        if len(prices) < 2:
            return "Waiting for data…"
        logger.debug("Rendering graph for %s with %d points", sym, len(prices))
        width = max(10, self.size.width - 4)
        height = max(4, self.size.height - 6)
        fig = plotille.Figure()
        fig.width = width
        fig.height = height
        fig.set_x_limits(min_=0, max_=len(prices) - 1)
        lo, hi = min(prices), max(prices)
        if lo == hi:
            lo -= 1
            hi += 1
        fig.set_y_limits(min_=lo, max_=hi)
        fig.color_mode = None
        fig.plot(range(len(prices)), prices)
        return fig.show(legend=False)

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
