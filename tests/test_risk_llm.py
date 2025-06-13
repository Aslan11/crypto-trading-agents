import pytest

import agents.ensemble.ensemble_agent as ea

class DummyWorkflowHandle:
    async def result(self):
        return {"status": "APPROVED"}

class DummyClient:
    def __init__(self):
        self.handle = DummyWorkflowHandle()
    async def start_workflow(self, *args, **kwargs):
        return self.handle
    def get_workflow_handle(self, _):
        return self.handle

class DummyResp:
    def __init__(self, text: str):
        self.choices = [
            type(
                "Choice",
                (),
                {"message": type("Msg", (), {"content": text})()},
            )
        ]

class DummyOpenAIClient:
    def __init__(self, text: str):
        self.text = text
        self.chat = type("Chat", (), {"completions": self})()
    async def create(self, *args, **kwargs):
        return DummyResp(self.text)

class DummyOpenAI:
    def __init__(self, text):
        self.text = text
    def AsyncOpenAI(self):
        return DummyOpenAIClient(self.text)

async def fake_get_client():
    return DummyClient()

async def fake_get_ledger_status(_client):
    return {"cash": 1000.0, "positions": {}}

@pytest.mark.asyncio
async def test_risk_check_llm_approve(monkeypatch):
    monkeypatch.setattr(ea, "_get_client", fake_get_client)
    monkeypatch.setattr(ea, "_get_ledger_status", fake_get_ledger_status)
    monkeypatch.setattr(ea, "openai", DummyOpenAI("APPROVE: ok"))
    approved = await ea._risk_check(None, {"symbol": "BTC/USD", "side": "BUY", "qty": 1, "price": 10, "ts": 1})
    assert approved

@pytest.mark.asyncio
async def test_risk_check_llm_reject(monkeypatch):
    monkeypatch.setattr(ea, "_get_client", fake_get_client)
    monkeypatch.setattr(ea, "_get_ledger_status", fake_get_ledger_status)
    monkeypatch.setattr(ea, "openai", DummyOpenAI("REJECT: bad"))
    approved = await ea._risk_check(None, {"symbol": "BTC/USD", "side": "BUY", "qty": 1, "price": 10, "ts": 1})
    assert not approved
