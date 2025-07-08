from __future__ import annotations

import os
import logging
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.client import Client, RPCError, RPCStatusCode
from agents.workflows import ExecutionAgentWorkflow

MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

@activity.defn
async def record_nudge(payload: dict) -> None:
    """Signal the execution-agent workflow with the provided timestamp."""
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    wf_id = os.environ.get("EXECUTION_WF_ID", "execution-agent")
    client = await Client.connect(address, namespace=namespace)
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("nudge", payload.get("ts"))
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                ExecutionAgentWorkflow.run,
                id=wf_id,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
            )
            await handle.signal("nudge", payload.get("ts"))
        else:
            raise

@workflow.defn
class EnsembleNudgeWorkflow:
    @workflow.run
    async def run(self) -> dict:
        ts = int(workflow.now().timestamp())
        payload = {"ts": ts}
        await workflow.execute_activity(
            record_nudge,
            payload,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return payload
