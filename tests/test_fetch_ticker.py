import asyncio
import pytest

from tools.market_data import fetch_ticker


class DummyExchange:
    def __init__(self):
        self.symbols = ["BTC/USD"]

    async def load_markets(self):
        pass

    async def fetch_ticker(self, symbol):
        return {"symbol": symbol}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_fetch_ticker_invalid_symbol(monkeypatch):
    import ccxt.async_support as ccxt
    monkeypatch.setattr(ccxt, "coinbaseexchange", DummyExchange)
    with pytest.raises(ValueError):
        await fetch_ticker("coinbaseexchange", "ETH/USD")
