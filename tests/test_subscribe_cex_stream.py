import asyncio
import os
import sitecustomize  # ensures docker_service is available
from temporalio.testing import docker_service

import importlib
import mcp_server.app as mapp
from worker.main import main as worker_main


def test_subscribe_cex_stream():
    async def run_test():
        async with docker_service() as svc:
            os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
            os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
            os.environ["DISABLE_TEMPORAL"] = "1"
            await worker_main()
            importlib.reload(mapp)
            result = await mapp.subscribe_cex_stream(symbols=["BTC/USD"], interval_sec=0.1)
            workflow_id = result["workflow_id"]
            run_id = result["run_id"]

            class Req:
                def __init__(self, wf, run):
                    self.path_params = {"workflow_id": wf, "run_id": run}

            payload = await mapp.workflow_status(Req(workflow_id, run_id))
            assert payload["status"] == "COMPLETED"
            assert payload["result"] is None

    asyncio.run(run_test())
