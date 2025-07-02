"""Workflow for prompting agents via the MCP signal log."""

from __future__ import annotations

import aiohttp
import os
import logging
from datetime import timedelta
import time

from temporalio import activity, workflow

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

logger = logging.getLogger(__name__)


@activity.defn
async def emit_prompt(payload: dict) -> None:
    """POST a prompt payload to the MCP server signal log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/agent_prompt"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=payload)
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("Failed to emit agent prompt: %s", exc)


@workflow.defn
class AgentPromptWorkflow:
    """Workflow that records a prompt for another agent."""

    @workflow.run
    async def run(self, target: str, message: str) -> dict:
        payload = {
            "ts": int(time.time()),
            "target": target,
            "message": message,
        }
        await workflow.execute_activity(
            emit_prompt,
            payload,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return payload
