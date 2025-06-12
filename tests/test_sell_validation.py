import pytest

from agents.execution import mock_exec_agent as mea

class DummyHandle:
    async def query(self, what):
        if what == "get_cash":
            return 1000.0
        if what == "get_positions":
            return {}

class DummyClient:
    def get_workflow_handle(self, wf_id):
        return DummyHandle()

aSYNC_CLIENT = DummyClient()

async def fake_get_client():
    return aSYNC_CLIENT

async def fake_ensure(_):
    return None

class DummySession:
    def __init__(self):
        self.post_called = False
    async def post(self, *args, **kwargs):
        self.post_called = True
    async def get(self, *args, **kwargs):
        return None

@pytest.mark.asyncio
async def test_sell_with_no_position_skips_order(monkeypatch):
    monkeypatch.setattr(mea, "_get_client", fake_get_client)
    monkeypatch.setattr(mea, "_ensure_workflow", fake_ensure)
    sess = DummySession()
    intent = {"side": "SELL", "symbol": "BTC/USD", "qty": 1, "price": 10}
    await mea._place_order(sess, intent)
    assert not sess.post_called
