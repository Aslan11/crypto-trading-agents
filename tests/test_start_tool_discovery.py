import types

from fastapi.testclient import TestClient
import mcp_server.app as mapp


class DummyHandle:
    run_id = "run"
    result_run_id = "run"


class DummyClient:
    async def start_workflow(self, *args, **kwargs):
        return DummyHandle()


def test_start_tool_uses_cached_workflows(monkeypatch):
    calls = {"count": 0}

    def fake_discover():
        calls["count"] += 1

        async def dummy_tool() -> None:
            return None

        return {"DummyTool": dummy_tool}

    async def fake_get_client():
        return DummyClient()

    monkeypatch.setattr(mapp, "_discover_workflows", fake_discover)
    monkeypatch.setattr(mapp, "get_temporal_client", fake_get_client)

    # Simulate lifespan startup
    mapp.workflows = fake_discover()
    mapp.client = None

    client = TestClient(mapp.app.streamable_http_app())
    resp = client.post("/tools/DummyTool", json={})
    assert resp.status_code == 202
    assert calls["count"] == 1

