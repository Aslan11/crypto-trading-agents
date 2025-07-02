import os
import asyncio
from datetime import timedelta


from temporalio import workflow, activity
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.service import RPCError, RPCStatusCode

import aiohttp
import time

# Signal name used by the ensemble agent to know when to run the prompt
ENSEMBLE_PROMPT_SIGNAL = "ensemble_prompt"

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "mcp-tools")
SERVER_URL = os.environ.get("MCP_SERVER", "http://localhost:8080")

_client: Client | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> Client:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = await Client.connect(
                    TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE
                )
    return _client


@activity.defn
async def prompt_ensemble_agent(symbol: str) -> None:
    """Send a nudge prompting the ensemble agent to evaluate the market."""

    url = f"{SERVER_URL.rstrip('/')}/signal/{ENSEMBLE_PROMPT_SIGNAL}"
    payload = {"symbol": symbol, "ts": int(time.time())}
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            await session.post(url, json=payload)
        except Exception:
            # Network errors are non-fatal for this best-effort signal
            pass


@workflow.defn
class EnsembleDecisionWorkflow:
    @workflow.run
    async def run(self, symbol: str = "BTC/USD") -> None:
        await workflow.execute_activity(
            prompt_ensemble_agent,
            args=[symbol],
            schedule_to_close_timeout=timedelta(seconds=5),
        )


async def ensure_schedule() -> None:
    client = await _get_client()
    schedule_id = "ensemble-prompt-schedule"
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.describe()
        return
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            id="ensemble-prompt-workflow",
            workflow="agents.ensemble_schedule.EnsembleDecisionWorkflow.run",
            args=[os.environ.get("TRADE_SYMBOL", "BTC/USD")],
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=30))]
        ),
    )
    await client.create_schedule(schedule_id, schedule, trigger_immediately=True)


async def main() -> None:
    await ensure_schedule()


if __name__ == "__main__":
    asyncio.run(main())
