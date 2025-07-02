import asyncio
import os
import sitecustomize  # ensures docker_service is available
from temporalio.testing import docker_service
from fastapi.testclient import TestClient

import importlib
import mcp_server.app as mapp
from worker.main import main as worker_main
import pytest


@pytest.mark.asyncio
async def test_start_market_stream():
    async with docker_service() as svc:
        os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
        os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
        os.environ["DISABLE_TEMPORAL"] = "1"
        await worker_main()
        importlib.reload(mapp)
        with TestClient(mapp.app.streamable_http_app()) as client:
            resp = client.post(
                "/tools/start_market_stream",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "start_market_stream",
                    "params": {"symbols": ["BTC/USD"], "interval_sec": 0.1},
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert resp.status_code == 202
            data = resp.json()
            workflow_id = data["workflow_id"]
            run_id = data["run_id"]
            result = None
            status = None
            for _ in range(50):
                status_resp = client.get(f"/workflow/{workflow_id}/{run_id}")
                assert status_resp.status_code == 200
                payload = status_resp.json()
                status = payload["status"]
                if status != "RUNNING":
                    result = payload.get("result")
                    break
                await asyncio.sleep(0.1)
            assert status == "COMPLETED"
            assert result is None
