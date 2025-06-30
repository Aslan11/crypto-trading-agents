from __future__ import annotations

"""Simple ASCII line chart with 11 vertical rows and sliding window."""

from collections import deque
from typing import Iterable, Deque, List


class AsciiLineChart:
    """Render a sliding window of prices as a Unicode line chart."""

    def __init__(self, width: int = 50, color: bool = False) -> None:
        self.width = width
        self.color = color
        self.prices: Deque[float] = deque(maxlen=width)

    def add(self, price: float) -> None:
        """Append ``price`` to the chart window."""
        self.prices.append(float(price))

    # mapping helpers -----------------------------------------------------
    @staticmethod
    def _map_rows(prices: Iterable[float]) -> List[int]:
        prices = list(prices)
        if not prices:
            return []
        hi = max(prices)
        lo = min(prices)
        if hi == lo:
            return [5] * len(prices)
        scale = 10.0 / (hi - lo)
        return [int(round((hi - p) * scale)) for p in prices]

    def _colorize(self, ch: str, direction: int) -> str:
        if not self.color or direction == 0:
            return ch
        if direction > 0:
            return "\033[32m" + ch + "\033[0m"
        return "\033[31m" + ch + "\033[0m"

    # rendering -----------------------------------------------------------
    def render(self) -> str:
        """Return the current chart as a string."""
        if not self.prices:
            return ""

        prices = list(self.prices)
        rows = self._map_rows(prices)
        hi = max(prices)
        lo = min(prices)
        avg = (hi + lo) / 2

        def _fmt(v: float) -> str:
            return str(int(round(v)))

        digits = max(len(_fmt(hi)), len(_fmt(avg)), len(_fmt(lo)))
        labels = {
            0: f"H {_fmt(hi):>{digits}} ",
            5: f"A {_fmt(avg):>{digits}} ",
            10: f"L {_fmt(lo):>{digits}} ",
        }
        prefix_width = len(f"L {_fmt(lo):>{digits}} ")

        row_prefix = [
            labels.get(i, " " * prefix_width)
            + ("┤" if i in (0, 5) else "┼" if i == 10 else "│")
            for i in range(11)
        ]

        grid: List[List[str]] = [[" "] * self.width for _ in range(11)]
        offset = self.width - len(rows)

        for idx in range(len(rows) - 1):
            r1 = rows[idx]
            r2 = rows[idx + 1]
            if r2 == r1:
                ch = "─"
                direction = 0
            elif r2 < r1:
                ch = "╮"
                direction = 1
            else:
                ch = "╯"
                direction = -1
            grid[r1][offset + idx] = self._colorize(ch, direction)

        last_row = rows[-1]
        if len(rows) > 1:
            prev_row = rows[-2]
            direction = 1 if last_row < prev_row else -1 if last_row > prev_row else 0
        else:
            direction = 0
        grid[last_row][offset + len(rows) - 1] = self._colorize("●", direction)

        lines = [row_prefix[i] + "".join(grid[i]) for i in range(11)]
        arrow_pad = prefix_width + 1
        lines.append(" " * arrow_pad + "► Time →")
        lines.append(" " * arrow_pad + f"t-{self.width - 1}…t0")
        return "\n".join(lines)
