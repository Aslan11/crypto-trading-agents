import logging
import pytest
from temporalio.service import RPCError, RPCStatusCode

from agents.ensemble import ensemble_agent as ea


class DummyHandle:
    def __init__(self):
        self.signal_calls = 0

    async def signal(self, *args, **kwargs):
        self.signal_calls += 1
        raise RPCError("boom", RPCStatusCode.INTERNAL, b"")


class DummyClient:
    def __init__(self):
        self.handle = DummyHandle()

    def get_workflow_handle(self, wf_id: str):
        return self.handle


async def fake_get_client():
    return DummyClient()


async def fake_ensure(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_poll_vectors_logs_rpc_error_and_continues(monkeypatch, caplog):
    calls = {"count": 0}

    async def fake_fetch(_session, _url, *, params=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return [{"symbol": "BTC/USD", "ts": 1, "data": {"mid": 1.0}}]
        ea.STOP_EVENT.set()
        return []

    monkeypatch.setattr(ea, "_fetch", fake_fetch)
    monkeypatch.setattr(ea, "_get_client", fake_get_client)
    monkeypatch.setattr(ea, "_ensure_workflow", fake_ensure)
    ea.STOP_EVENT.clear()

    with caplog.at_level(logging.WARNING):
        await ea._poll_vectors(None)

    assert calls["count"] >= 2
    assert "RPC error" in caplog.text


class DummyHandleExc:
    def __init__(self):
        self.signal_calls = 0

    async def signal(self, *args, **kwargs):
        self.signal_calls += 1
        raise ValueError("boom")


class DummyClientExc:
    def __init__(self):
        self.handle = DummyHandleExc()

    def get_workflow_handle(self, wf_id: str):
        return self.handle


async def fake_get_client_exc():
    return DummyClientExc()


@pytest.mark.asyncio
async def test_poll_vectors_logs_unexpected_error_and_continues(monkeypatch, caplog):
    calls = {"count": 0}

    async def fake_fetch(_session, _url, *, params=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return [{"symbol": "BTC/USD", "ts": 1, "data": {"mid": 1.0}}]
        ea.STOP_EVENT.set()
        return []

    monkeypatch.setattr(ea, "_fetch", fake_fetch)
    monkeypatch.setattr(ea, "_get_client", fake_get_client_exc)
    monkeypatch.setattr(ea, "_ensure_workflow", fake_ensure)
    ea.STOP_EVENT.clear()

    with caplog.at_level(logging.ERROR):
        await ea._poll_vectors(None)

    assert calls["count"] >= 2
    assert "Unexpected error updating price" in caplog.text


class DummyQueryHandle:
    def __init__(self):
        self.query_calls = 0
        self.signal_calls = 0

    async def query(self, *args, **kwargs):
        self.query_calls += 1
        raise RPCError("boom", RPCStatusCode.INTERNAL, b"")

    async def signal(self, *args, **kwargs):
        self.signal_calls += 1
        return None


class DummyQueryClient:
    def __init__(self):
        self.handle = DummyQueryHandle()

    def get_workflow_handle(self, wf_id: str):
        return self.handle


async def fake_get_client_sig():
    return DummyQueryClient()


async def fake_risk_check(_session, _intent):
    return True


async def fake_broadcast(_client, _intent):
    return None


@pytest.mark.asyncio
async def test_poll_signals_logs_rpc_error_and_continues(monkeypatch, caplog):
    calls = {"count": 0}

    async def fake_fetch(_session, _url, *, params=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return [{"symbol": "BTC/USD", "side": "BUY", "ts": 1}]
        ea.STOP_EVENT.set()
        return []

    monkeypatch.setattr(ea, "_fetch", fake_fetch)
    monkeypatch.setattr(ea, "_get_client", fake_get_client_sig)
    monkeypatch.setattr(ea, "_ensure_workflow", fake_ensure)
    monkeypatch.setattr(ea, "_risk_check", fake_risk_check)
    monkeypatch.setattr(ea, "_broadcast_intent", fake_broadcast)
    ea.STOP_EVENT.clear()

    with caplog.at_level(logging.WARNING):
        await ea._poll_signals(None)

    assert calls["count"] >= 2
    assert "RPC error" in caplog.text


class DummyQueryHandleExc:
    def __init__(self):
        self.query_calls = 0
        self.signal_calls = 0

    async def query(self, *args, **kwargs):
        self.query_calls += 1
        raise ValueError("boom")

    async def signal(self, *args, **kwargs):
        self.signal_calls += 1
        return None


class DummyQueryClientExc:
    def __init__(self):
        self.handle = DummyQueryHandleExc()

    def get_workflow_handle(self, wf_id: str):
        return self.handle


async def fake_get_client_sig_exc():
    return DummyQueryClientExc()


@pytest.mark.asyncio
async def test_poll_signals_logs_unexpected_error_and_continues(monkeypatch, caplog):
    calls = {"count": 0}

    async def fake_fetch(_session, _url, *, params=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return [{"symbol": "BTC/USD", "side": "BUY", "ts": 1}]
        ea.STOP_EVENT.set()
        return []

    monkeypatch.setattr(ea, "_fetch", fake_fetch)
    monkeypatch.setattr(ea, "_get_client", fake_get_client_sig_exc)
    monkeypatch.setattr(ea, "_ensure_workflow", fake_ensure)
    monkeypatch.setattr(ea, "_risk_check", fake_risk_check)
    monkeypatch.setattr(ea, "_broadcast_intent", fake_broadcast)
    ea.STOP_EVENT.clear()

    with caplog.at_level(logging.ERROR):
        await ea._poll_signals(None)

    assert calls["count"] >= 2
    assert "Unexpected error processing intent" in caplog.text
