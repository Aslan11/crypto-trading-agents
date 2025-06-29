import pytest
from fastapi.testclient import TestClient
from mcp_server.app import app


def test_momentum_stop_signal_records_symbol():
    client = TestClient(app)
    resp = client.post("/tools/stop_momentum_pair", json={"symbol": "BTC"})
    assert resp.status_code == 200
    fetch = client.get("/signal/momentum_stop")
    assert fetch.status_code == 200
    events = fetch.json()
    assert any(e.get("symbol") == "BTC/USD" for e in events)
