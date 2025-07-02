import os
import json
import asyncio
from typing import Any, AsyncIterator
import aiohttp
import openai
import secrets
from datetime import timedelta
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent
from temporalio.client import (
    Client,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.service import RPCError, RPCStatusCode
from tools.ensemble_nudge import EnsembleNudgeWorkflow
import time

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a strategy ensemble agent. Every 30 seconds you are nudged to review "
    "the portfolio and recent market data. "
    "Use the available tools to fetch portfolio status, evaluate risk and, if prudent, "
    "place mock orders. Always call `get_portfolio_status` before trading and avoid "
    "selling below the entry price whenever possible."
)


def _tool_result_data(result: Any) -> Any:
    """Return JSON-friendly data from a tool call result."""
    if isinstance(result, CallToolResult):
        if result.content:
            parsed: list[Any] = []
            for item in result.content:
                if isinstance(item, TextContent):
                    try:
                        parsed.append(json.loads(item.text))
                    except Exception:
                        parsed.append(item.text)
                else:
                    parsed.append(item.model_dump() if hasattr(item, "model_dump") else item)
            return parsed if len(parsed) > 1 else parsed[0]
        return []
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result

async def _latest_price(
    session: aiohttp.ClientSession, base_url: str, symbol: str
) -> float:
    """Return the most recent market price for ``symbol``."""
    url = base_url.rstrip("/") + "/signal/market_tick"
    params = {"after": int(time.time()) - 60}
    headers = {"Accept": "text/event-stream"}
    last_price = 0.0
    end_time = asyncio.get_event_loop().time() + 2
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                return 0.0
            while asyncio.get_event_loop().time() < end_time:
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
                if evt.get("symbol") != symbol:
                    continue
                data = evt.get("data", {})
                if "last" in data:
                    last_price = float(data["last"])
                elif {"bid", "ask"}.issubset(data):
                    last_price = (float(data["bid"]) + float(data["ask"])) / 2
    except Exception:
        return 0.0

    return last_price

async def _stream_nudges(
    session: aiohttp.ClientSession, base_url: str
) -> AsyncIterator[dict]:
    """Yield ensemble nudge events from the MCP server."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/ensemble_nudge"
    headers = {"Accept": "text/event-stream"}

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
                    ts = evt.get("ts")
                    if ts is not None:
                        cursor = max(cursor, ts)
                    yield evt
        except Exception:
            await asyncio.sleep(1)


async def _ensure_schedule(client: Client) -> None:
    """Create the ensemble nudge schedule if it doesn't exist."""
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

async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the ensemble agent and react to strategy signals."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp"

    # Ensure the periodic nudge schedule exists
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(address, namespace=namespace)
    await _ensure_schedule(client)

    # Streaming strategy signals requires an indefinite timeout
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with streamablehttp_client(mcp_url) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                tools = tools_resp.tools
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
                print(
                    "[EnsembleAgent] Connected to MCP server with tools:",
                    [t.name for t in tools],
                )

                async for _ in _stream_nudges(http_session, base_url):
                    status_result = await session.call_tool("get_portfolio_status", {})
                    status = _tool_result_data(status_result)
                    signal_str = (
                        f"Nudge received. Current portfolio: {json.dumps(status)}. "
                        "Review market data and decide whether to trade."
                    )
                    conversation.append({"role": "user", "content": signal_str})
                    openai_tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.inputSchema,
                            },
                        }
                        for tool in tools
                    ]
                    while True:
                        response = openai_client.chat.completions.create(
                            model=os.environ.get("OPENAI_MODEL", "o4-mini"),
                            messages=conversation,
                            tools=openai_tools,
                            tool_choice="auto",
                        )
                        msg = response.choices[0].message

                        # Newer versions of the OpenAI SDK return a ChatCompletionMessage
                        # object. Inspect its attributes instead of treating it like a
                        # dictionary.
                        if getattr(msg, "tool_calls", None):
                            conversation.append(
                                {
                                    "role": msg.role,
                                    "content": msg.content,
                                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                                }
                            )
                            for tool_call in msg.tool_calls:
                                func_name = tool_call.function.name
                                func_args = json.loads(
                                    tool_call.function.arguments or "{}"
                                )
                                print(
                                    f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                                )
                                result = await session.call_tool(func_name, func_args)
                                conversation.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "name": func_name,
                                        "content": json.dumps(result.model_dump()),
                                    }
                                )
                            continue

                        if getattr(msg, "function_call", None):
                            conversation.append(
                                {
                                    "role": msg.role,
                                    "content": msg.content,
                                    "function_call": msg.function_call.model_dump(),
                                }
                            )
                            func_name = msg.function_call.name
                            func_args = json.loads(msg.function_call.arguments or "{}")
                            print(
                                f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                            )
                            result = await session.call_tool(func_name, func_args)
                            conversation.append(
                                {
                                    "role": "function",
                                    "name": func_name,
                                    "content": json.dumps(result.model_dump()),
                                }
                            )
                            continue

                        assistant_reply = msg.content or ""
                        conversation.append(
                            {"role": "assistant", "content": assistant_reply}
                        )
                        print(f"{PINK}[EnsembleAgent] Decision: {assistant_reply}{RESET}")
                        conversation = [
                            {"role": "system", "content": SYSTEM_PROMPT}
                        ]
                        break

if __name__ == "__main__":
    asyncio.run(run_ensemble_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
