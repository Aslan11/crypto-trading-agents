from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

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

