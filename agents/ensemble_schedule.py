"""Create a Temporal schedule that nudges the ensemble agent every 30 seconds."""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import timedelta

from temporalio.client import (
    Client,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.service import RPCError, RPCStatusCode

from tools.ensemble_nudge import EnsembleNudgeWorkflow


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(address, namespace=namespace)

    sched_id = "ensemble-nudge-schedule"
    handle = client.get_schedule_handle(sched_id)
    try:
        await handle.describe()
        return
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise

    await client.create_schedule(
        id=sched_id,
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=30))]
        ),
        action=ScheduleActionStartWorkflow(
            EnsembleNudgeWorkflow.run,
            id=f"nudge-{secrets.token_hex(4)}",
            task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
