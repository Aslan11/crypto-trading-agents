import asyncio
import os
from temporalio.testing import docker_service
from fastapi.testclient import TestClient

from mcp_server.app import app
from worker.main import main as worker_main
import pytest


@pytest.mark.asyncio
async def test_subscribe_cex_stream():
    async with docker_service() as svc:
        os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
        os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
        worker_task = asyncio.create_task(worker_main())
        client = TestClient(app)
        try:
            resp = client.post(
                "/tools/SubscribeCEXStream",
                json={
                    "exchange": "binance",
                    "symbols": ["BTC/USDT"],
                    "interval_sec": 0.1,
                    "max_cycles": 2,
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
                    result = payload["result"]
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
