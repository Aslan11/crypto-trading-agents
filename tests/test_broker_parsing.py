import pytest

from agents.broker_agent_client import _parse_symbols


def test_parse_symbols_handles_names():
    pairs = _parse_symbols("How about Ethereum & Bitcoin?")
    assert "ETH/USD" in pairs
    assert "BTC/USD" in pairs
