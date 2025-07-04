"""Workflows that ping the ensemble agent via the MCP signal log."""

from __future__ import annotations

import logging
import os
from datetime import timedelta

import aiohttp
from temporalio import activity, workflow

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@activity.defn
async def emit_nudge() -> None:
    """Send a nudge event to the ensemble agent via the MCP signal log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/ensemble_nudge"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json={})
        except Exception as exc:
            logger.error("Failed to emit nudge: %s", exc)


@workflow.defn
class SendEnsembleNudge:
    """Workflow that simply emits a nudge event."""

    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            emit_nudge,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
