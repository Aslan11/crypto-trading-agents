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
from typing import Any, AsyncIterator, Optional

import aiohttp
import asyncio
import json
import logging


def format_log(data: Any) -> str:
    """Return a pretty string representation of ``data`` for logging."""
    if isinstance(data, str):
        return data
    return pformat(data, width=60)



async def sse_events(
    session: aiohttp.ClientSession,
    url: str,
    *,
    after: int = 0,
    stop: Optional[asyncio.Event] = None,
    log: Optional[logging.Logger] = None,
) -> AsyncIterator[dict]:
    """Yield events from ``url`` using server-sent events."""

    stop_event = stop or asyncio.Event()
    logger = log or logging.getLogger(__name__)
    cursor = after
    headers = {"Accept": "text/event-stream"}

    while not stop_event.is_set():
        params = {"after": cursor}
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("SSE error %s", resp.status)
                    await asyncio.sleep(1)
                    continue

                buffer = ""
                data_buf = ""
                async for chunk in resp.content.iter_any():
                    if stop_event.is_set():
                        break
                    buffer += chunk.decode()
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip()
                        if line.startswith("data:"):
                            data_buf += line[5:].strip()
                        elif line == "":
                            if data_buf:
                                try:
                                    event = json.loads(data_buf)
                                except Exception:
                                    event = None
                                data_buf = ""
                                if event is not None:
                                    yield event
                                    if "ts" in event:
                                        cursor = max(cursor, int(event["ts"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("SSE connection failed: %s", exc)
            await asyncio.sleep(1)


__all__ = ["print_banner", "format_log", "sse_events"]

