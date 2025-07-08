import asyncio
import os
import pytest
from temporalio.testing import WorkflowEnvironment
from fastapi.testclient import TestClient

from mcp_server.app import app
from worker.main import main as worker_main

from tools.ensemble_nudge import EnsembleNudgeWorkflow


@pytest.mark.asyncio
async def test_broker_workflow_receives_symbols():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        os.environ["TEMPORAL_ADDRESS"] = env.client.config()["target"]
        os.environ["TEMPORAL_NAMESPACE"] = env.client.namespace
        worker_task = asyncio.create_task(worker_main())
        client = TestClient(app)
        try:
            resp = client.post(
                "/tools/start_market_stream",
                json={"symbols": ["BTC/USD", "ETH/USD"], "interval_sec": 0.1},
            )
            assert resp.status_code == 202
            handle = env.client.get_workflow_handle(os.environ.get("BROKER_WF_ID", "broker-agent"))
            # Allow signals to be processed
            await env.sleep(1)
            symbols = await handle.query("get_symbols")
            assert set(symbols) == {"BTC/USD", "ETH/USD"}
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_execution_workflow_nudge():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        os.environ["TEMPORAL_ADDRESS"] = env.client.config()["target"]
        os.environ["TEMPORAL_NAMESPACE"] = env.client.namespace
        worker_task = asyncio.create_task(worker_main())
        try:
            handle = await env.client.start_workflow(
                EnsembleNudgeWorkflow.run,
                id="test-nudge",
                task_queue="mcp-tools",
            )
            await handle.result()
            exec_handle = env.client.get_workflow_handle(os.environ.get("EXECUTION_WF_ID", "execution-agent"))
            await env.sleep(1)
            nudges = await exec_handle.query("get_nudges")
            assert nudges and nudges[0] > 0
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
