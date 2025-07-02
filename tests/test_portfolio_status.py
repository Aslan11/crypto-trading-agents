import asyncio
import os

import sitecustomize  # ensures docker_service is available
from temporalio.testing import docker_service

import importlib
import mcp_server.app as mapp
from worker.main import main as worker_main


def test_get_portfolio_status_includes_pnl():
    async def run_test():
        async with docker_service() as svc:
            os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
            os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
            os.environ["DISABLE_TEMPORAL"] = "1"
            await worker_main()
            importlib.reload(mapp)
            data = await mapp.get_portfolio_status()
            assert "pnl" in data

    asyncio.run(run_test())
