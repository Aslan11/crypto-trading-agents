import os
import pytest
from temporalio.testing import WorkflowEnvironment

from agents.feature_engineering_service import (
    subscribe_vectors,
    STOP_EVENT,
    _store_vector,
)


@pytest.mark.asyncio
async def test_subscribe_vectors_respects_stop_event():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        os.environ["TEMPORAL_ADDRESS"] = env.client.config()["target"]
        os.environ["TEMPORAL_NAMESPACE"] = env.client.namespace
        STOP_EVENT.clear()

        await _store_vector("BTC/USD", 1, {"foo": "bar"})
        gen = subscribe_vectors("BTC/USD", use_local=True)

        first = await anext(gen)
        assert first == {"foo": "bar"}

        STOP_EVENT.set()
        with pytest.raises(StopAsyncIteration):
            await anext(gen)

        STOP_EVENT.clear()
