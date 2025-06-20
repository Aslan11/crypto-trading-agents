"""Utility helpers shared across agents."""

from __future__ import annotations


def print_banner(name: str, purpose: str) -> None:
    """Print a simple ASCII banner with ``name`` and ``purpose``."""
    lines = [name, purpose]
    width = max(len(line) for line in lines) + 4
    border = "*" * width
    print(border)
    for line in lines:
        print(f"* {line.ljust(width - 4)} *")
    print(border)

from pprint import pformat
from typing import Any


def format_log(data: Any) -> str:
    """Return a pretty string representation of ``data`` for logging."""
    if isinstance(data, str):
        return data
    return pformat(data, width=60)


__all__ = ["print_banner", "format_log"]

