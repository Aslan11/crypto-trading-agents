import asyncio
import os
from temporalio.testing import WorkflowEnvironment
from fastapi.testclient import TestClient

from mcp_server.app import app
from worker.main import main as worker_main
import pytest


@pytest.mark.asyncio
async def test_start_market_stream():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        os.environ["TEMPORAL_ADDRESS"] = env.client.config()["target"]
        os.environ["TEMPORAL_NAMESPACE"] = env.client.namespace
        worker_task = asyncio.create_task(worker_main())
        client = TestClient(app)
        try:
            resp = client.post(
                "/tools/start_market_stream",
                json={
                    "symbols": ["BTC/USD"],
                    "interval_sec": 0.1,
                },
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
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
