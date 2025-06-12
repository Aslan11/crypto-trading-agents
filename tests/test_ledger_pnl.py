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

