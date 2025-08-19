"""Mock execution workflows used by the sample agents."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, List, Literal, Union

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


class BatchOrderIntent(BaseModel):
    """Schema for batch order execution - supports single or multiple orders."""
    
    orders: List[OrderIntent]


@activity.defn
async def mock_fill(intent: OrderIntent) -> Dict:
    """Return a fill dict for the provided intent."""

    qty = float(intent.qty)
    price = float(intent.price)
    cost = qty * price
    logger.info("Mock fill for %s %s @%s", intent.side, qty, price)
    return {**intent.model_dump(), "fill_price": price, "cost": cost}


@activity.defn
async def mock_batch_fill(batch_intent: BatchOrderIntent) -> List[Dict]:
    """Return a list of fill dicts for batch orders."""
    
    results = []
    logger.info("Processing batch of %d orders", len(batch_intent.orders))
    
    for i, intent in enumerate(batch_intent.orders):
        qty = float(intent.qty)
        price = float(intent.price)
        cost = qty * price
        logger.info("Batch fill %d/%d: %s %s %s @%s", i+1, len(batch_intent.orders), intent.side, qty, intent.symbol, price)
        
        fill_result = {**intent.model_dump(), "fill_price": price, "cost": cost}
        results.append(fill_result)
    
    return results


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


@workflow.defn
class PlaceMockBatchOrder:
    """Workflow wrapper around :func:`mock_batch_fill`."""

    @workflow.run
    async def run(self, batch_intent: BatchOrderIntent) -> List[Dict]:
        logger.info("Placing batch mock order with %d orders", len(batch_intent.orders))
        result: List[Dict] = await workflow.execute_activity(
            mock_batch_fill,
            batch_intent,
            schedule_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return result

