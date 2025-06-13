import pytest
from agents.workflows import ExecutionLedgerWorkflow
from decimal import Decimal


def test_pnl_uses_initial_cash_and_unrealized_value():
    wf = ExecutionLedgerWorkflow()
    assert wf.get_pnl() == 0.0
    wf.record_fill({
        "side": "BUY",
        "symbol": "BTC/USD",
        "qty": 10,
        "fill_price": 1000,
        "cost": 10000,
    })
    assert wf.get_pnl() == 0.0
    wf.last_price["BTC/USD"] = Decimal("1100")
    assert wf.get_pnl() == pytest.approx(1000.0)


def test_entry_price_tracking():
    wf = ExecutionLedgerWorkflow()
    wf.record_fill({
        "side": "BUY",
        "symbol": "ETH/USD",
        "qty": 2,
        "fill_price": 100,
        "cost": 200,
    })
    assert wf.get_entry_prices()["ETH/USD"] == pytest.approx(100.0)
    wf.record_fill({
        "side": "BUY",
        "symbol": "ETH/USD",
        "qty": 2,
        "fill_price": 120,
        "cost": 240,
    })
    assert wf.get_entry_prices()["ETH/USD"] == pytest.approx(110.0)
    wf.record_fill({
        "side": "SELL",
        "symbol": "ETH/USD",
        "qty": 1,
        "fill_price": 130,
        "cost": 130,
    })
    assert wf.get_entry_prices()["ETH/USD"] == pytest.approx(110.0)
    wf.record_fill({
        "side": "SELL",
        "symbol": "ETH/USD",
        "qty": 3,
        "fill_price": 115,
        "cost": 345,
    })
    assert "ETH/USD" not in wf.get_entry_prices()

