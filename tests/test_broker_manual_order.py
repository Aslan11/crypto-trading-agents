import pytest
import agents.broker_agent as ba


class DummyResp:
    def __init__(self, text: str):
        self.choices = [type('Choice', (), {'message': type('Msg', (), {'content': text})()})]


class DummyClient:
    def __init__(self, text: str):
        self.text = text
        self.chat = type('Chat', (), {'completions': self})()

    async def create(self, *args, **kwargs):
        return DummyResp(self.text)


class DummyOpenAI:
    def __init__(self, text: str):
        self.text = text

    def AsyncOpenAI(self):
        return DummyClient(self.text)


@pytest.mark.asyncio
async def test_parse_order_llm(monkeypatch):
    text = '{"side": "BUY", "symbol": "BTC/USD", "qty": 1, "price": 100}'
    monkeypatch.setattr(ba, "openai", DummyOpenAI(text))
    intent = await ba._parse_order("please buy one bitcoin for 100 dollars")
    assert intent["side"] == "BUY"
    assert intent["symbol"] == "BTC/USD"
    assert intent["qty"] == 1
    assert intent["price"] == 100


@pytest.mark.asyncio
async def test_parse_order_none(monkeypatch):
    monkeypatch.setattr(ba, "openai", DummyOpenAI("{}"))
    assert await ba._parse_order("what's the weather?") is None
