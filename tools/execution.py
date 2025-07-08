"""Mock execution workflows used by the sample agents."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, Literal

from pydantic import BaseModel

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

class OrderIntent(BaseModel):
    """Schema for order intents sent to :func:`place_mock_order`."""

    symbol: str
    side: Literal["BUY", "SELL"]
    qty: float
    price: float
    type: Literal["market", "limit"]


@activity.defn
async def mock_fill(intent: OrderIntent) -> Dict:
    """Return a fill dict for the provided intent."""

    qty = float(intent.qty)
    price = float(intent.price)
    cost = qty * price
    logger.info("Mock fill for %s %s @%s", intent.side, qty, price)
    return {**intent.model_dump(), "fill_price": price, "cost": cost}


@workflow.defn
class PlaceMockOrder:
    """Workflow wrapper around :func:`mock_fill`."""

    @workflow.run
    async def run(self, intent: OrderIntent) -> Dict:
        logger.info("Placing mock order: %s", intent)
        result: Dict = await workflow.execute_activity(
            mock_fill,
            intent,
            schedule_to_close_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return result

