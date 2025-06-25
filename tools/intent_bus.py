"""Workflow that broadcasts approved intents to the MCP signal log."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import aiohttp
from temporalio import activity, workflow
import logging

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@activity.defn
async def emit_intent(intent: dict) -> None:
    """Send approved intent to the MCP signal log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/approved_intent"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=intent)
        except Exception as exc:
            logger.error("Failed to emit intent: %s", exc)


@workflow.defn
class IntentBus:
    """Workflow that broadcasts intents via the MCP signal endpoint."""

    def __init__(self) -> None:
        self._queue: list[dict] = []
        self._event = asyncio.Event()

    @workflow.signal
    def publish(self, intent: dict) -> None:
        self._queue.append(intent)
        self._event.set()

    @workflow.run
    async def run(self) -> None:
        while True:
            await workflow.wait_condition(lambda: bool(self._queue))
            intents = list(self._queue)
            self._queue.clear()
            for intent in intents:
                await workflow.execute_activity(
                    emit_intent,
                    intent,
                    schedule_to_close_timeout=timedelta(seconds=5),
                )
            await workflow.sleep(0)
