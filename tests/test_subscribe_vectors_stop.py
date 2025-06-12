import pytest

from agents.feature_engineering_agent import (
    subscribe_vectors,
    STOP_EVENT,
    _store_vector,
)


@pytest.mark.asyncio
async def test_subscribe_vectors_respects_stop_event():
    STOP_EVENT.clear()

    await _store_vector("BTC/USD", 1, {"foo": "bar"})
    gen = subscribe_vectors("BTC/USD", use_local=True)

    first = await anext(gen)
    assert first == {"foo": "bar"}

    STOP_EVENT.set()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)

    STOP_EVENT.clear()
