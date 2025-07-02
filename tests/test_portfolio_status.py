import asyncio
import os

import pytest
import sitecustomize  # ensures docker_service is available
from temporalio.testing import docker_service
from fastapi.testclient import TestClient

import importlib
import mcp_server.app as mapp
from worker.main import main as worker_main


@pytest.mark.asyncio
async def test_get_portfolio_status_includes_pnl():
    async with docker_service() as svc:
        os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
        os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
        os.environ["DISABLE_TEMPORAL"] = "1"
        await worker_main()
        importlib.reload(mapp)
        with TestClient(mapp.app.streamable_http_app()) as client:
            resp = client.post(
                "/tools/get_portfolio_status",
                json={"jsonrpc": "2.0", "id": 1, "method": "get_portfolio_status", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "pnl" in data
