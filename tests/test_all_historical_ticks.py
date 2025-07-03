import asyncio
import os
from datetime import datetime

import pytest
from temporalio.testing import docker_service
from temporalio.client import Client
from fastapi.testclient import TestClient

from mcp_server.app import app
from worker.main import main as worker_main
from tools.feature_engineering import ComputeFeatureVector


@pytest.mark.asyncio
async def test_get_all_historical_ticks():
    async with docker_service() as svc:
        os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
        os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
        worker_task = asyncio.create_task(worker_main())
        client = TestClient(app)
        try:
            temporal = await Client.connect(
                os.environ["TEMPORAL_ADDRESS"],
                namespace=os.environ["TEMPORAL_NAMESPACE"],
            )
            history = [
                {
                    "timestamp": int(datetime.utcnow().timestamp() * 1000) - 60000,
                    "last": 100.0,
                },
                {"timestamp": int(datetime.utcnow().timestamp() * 1000), "last": 101.0},
            ]
            await temporal.start_workflow(
                ComputeFeatureVector.run,
                args=["BTC/USD", 60, 3600, 9000, history],
                id="feature-BTC-USD",
                task_queue="mcp-tools",
            )

            resp = client.post(
                "/tools/get_all_historical_ticks",
                json={"symbols": ["BTC/USD"]},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "BTC/USD" in data
            assert len(data["BTC/USD"]) == 2
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
