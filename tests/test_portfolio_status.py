import asyncio
import os

import pytest
from temporalio.testing import docker_service
from fastapi.testclient import TestClient

from mcp_server.app import app
from worker.main import main as worker_main


@pytest.mark.asyncio
async def test_get_portfolio_status_includes_pnl():
    async with docker_service() as svc:
        os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
        os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
        worker_task = asyncio.create_task(worker_main())
        client = TestClient(app)
        try:
            resp = client.post("/tools/get_portfolio_status", json={})
            assert resp.status_code == 200
            data = resp.json()
            assert "pnl" in data
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
