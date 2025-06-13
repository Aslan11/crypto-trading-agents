import pytest

from agents.workflows import FeatureStoreWorkflow


def test_feature_store_queries():
    wf = FeatureStoreWorkflow()
    # Add vectors out of order for two symbols
    wf.add_vector("BTC", 2, {"v": 2})
    wf.add_vector("BTC", 1, {"v": 1})
    wf.add_vector("ETH", 5, {"v": 5})

    assert wf.latest_vector("BTC") == {"v": 2}
    assert wf.latest_vector("ETH") == {"v": 5}
    assert wf.latest_vector("XRP") is None

    # Retrieve next vectors
    assert wf.next_vector("BTC", 0) == (1, {"v": 1})
    assert wf.next_vector("BTC", 1) == (2, {"v": 2})
    assert wf.next_vector("BTC", 2) is None
    assert wf.next_vector("ETH", 4) == (5, {"v": 5})
    assert wf.next_vector("ETH", 5) is None

