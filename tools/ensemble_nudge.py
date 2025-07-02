"""Workflow to emit periodic ensemble nudges via the MCP server."""

from __future__ import annotations

import aiohttp
import os
import logging
from datetime import timedelta

from temporalio import activity, workflow

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

logger = logging.getLogger(__name__)


@activity.defn
async def emit_nudge(payload: dict) -> None:
    """POST the nudge payload to the MCP server signal log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/ensemble_nudge"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=payload)
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("Failed to emit ensemble nudge: %s", exc)


@workflow.defn
class EnsembleNudgeWorkflow:
    """Workflow that records a simple nudge event."""

    @workflow.run
    async def run(self) -> None:
        payload = {"ts": int(workflow.now().timestamp())}
        await workflow.execute_activity(
            emit_nudge,
            payload,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
