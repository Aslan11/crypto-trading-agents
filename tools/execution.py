"""Mock execution workflows used by the sample agents."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict
import os

import aiohttp
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

@activity.defn
async def mock_fill(intent: Dict) -> Dict:
    """Return a fill dict for the provided intent."""
    qty = intent["qty"]
    price = intent["price"]
    cost = qty * price
    logger.info("Mock fill for %s %s @%s", intent["side"], qty, price)
    return {**intent, "fill_price": price, "cost": cost}


@workflow.defn
class PlaceMockOrder:
    """Workflow wrapper around :func:`mock_fill`."""

    @workflow.run
    async def run(self, intent: Dict) -> Dict:
        logger.info("Placing mock order: %s", intent)
        result: Dict = await workflow.execute_activity(
            mock_fill,
            intent,
            schedule_to_close_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return result


@activity.defn
async def record_fill_event(fill: Dict) -> None:
    """Send executed fill to the MCP signal log."""
    url = f"http://{MCP_HOST}:{MCP_PORT}/signal/trade_signal"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=fill)
        except Exception as exc:
            logger.error("Failed to record fill: %s", exc)

