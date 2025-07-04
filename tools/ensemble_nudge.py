from __future__ import annotations

import os
import logging
from datetime import timedelta
import aiohttp
from temporalio import activity, workflow

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

@activity.defn
async def record_nudge(payload: dict) -> None:
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/ensemble_nudge"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=payload)
        except Exception as exc:
            logger.error("Failed to record nudge: %s", exc)

@workflow.defn
class EnsembleNudgeWorkflow:
    @workflow.run
    async def run(self) -> dict:
        ts = int(workflow.now().timestamp())
        payload = {"ts": ts}
        await workflow.execute_activity(
            record_nudge,
            payload,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return payload
