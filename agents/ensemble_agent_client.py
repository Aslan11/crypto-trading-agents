import asyncio
import json
import os

import aiohttp
from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleIntervalSpec, ScheduleSpec
from temporalio.service import RPCError, RPCStatusCode

from agents.workflows import EnsembleWorkflow

ENSEMBLE_WF_ID = "ensemble-agent"
SCHEDULE_ID = "ensemble-nudge"

async def _ensure_workflow(client: Client) -> None:
    handle = client.get_workflow_handle(ENSEMBLE_WF_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            await client.start_workflow(
                EnsembleWorkflow.run,
                id=ENSEMBLE_WF_ID,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
            )
        else:
            raise

async def _ensure_schedule(client: Client) -> None:
    handle = client.get_schedule_handle(SCHEDULE_ID)
    try:
        await handle.describe()
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                EnsembleWorkflow.run,
                id=ENSEMBLE_WF_ID,
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
                start_signal="nudge",
            ),
            spec=ScheduleSpec(
                intervals=[ScheduleIntervalSpec(every=30)]
            ),
        )
        await client.create_schedule(SCHEDULE_ID, schedule)
        print("[EnsembleAgent] Created Temporal Schedule ensemble-nudge (every 30s)")

async def _stream_ticks(client: Client, base_url: str) -> None:
    handle = client.get_workflow_handle(ENSEMBLE_WF_ID)
    cursor = 0
    url = base_url.rstrip("/") + "/signal/market_tick"
    headers = {"Accept": "text/event-stream"}
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(url, params={"after": cursor}, headers=headers) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(1)
                        continue
                    while True:
                        line = await resp.content.readline()
                        if not line:
                            break
                        text = line.decode().strip()
                        if not text or not text.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(text[5:].strip())
                        except Exception:
                            continue
                        symbol = evt.get("symbol")
                        data = evt.get("data", {})
                        ts = evt.get("ts")
                        price = None
                        if "last" in data:
                            price = float(data["last"])
                        elif {"bid", "ask"}.issubset(data):
                            price = (float(data["bid"]) + float(data["ask"])) / 2
                        if symbol and price is not None and ts is not None:
                            await handle.signal("update_price", symbol, price, ts)
                            cursor = max(cursor, ts)
            except Exception:
                await asyncio.sleep(1)

async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(address, namespace=namespace)
    await _ensure_workflow(client)
    await _ensure_schedule(client)
    await _stream_ticks(client, os.environ.get("MCP_SERVER", "http://localhost:8080"))

if __name__ == "__main__":
    asyncio.run(main())
