import asyncio
import os

import pytest
from temporalio.testing import WorkflowEnvironment
from fastapi.testclient import TestClient

from mcp_server.app import app
from worker.main import main as worker_main


@pytest.mark.asyncio
async def test_get_portfolio_status_includes_pnl():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        os.environ["TEMPORAL_ADDRESS"] = env.client.config()["target"]
        os.environ["TEMPORAL_NAMESPACE"] = env.client.namespace
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
