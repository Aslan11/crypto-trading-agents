import os
import json
import asyncio
from typing import Any, AsyncIterator
from contextlib import suppress
import aiohttp
import openai
import secrets
from datetime import timedelta
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.service import RPCError, RPCStatusCode
from mcp.types import CallToolResult, TextContent
from tools.ensemble_nudge import EnsembleNudgeWorkflow

ALLOWED_TOOLS = {
    "place_mock_order",
    "sign_and_send_tx",
    "get_historical_ticks",
    "get_portfolio_status",
}

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


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
                    parsed.append(
                        item.model_dump() if hasattr(item, "model_dump") else item
                    )
            return parsed if len(parsed) > 1 else parsed[0]
        return []
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result

SYSTEM_PROMPT = (
    "You are a strategy ensemble agent. Every 30 seconds you are nudged to review "
    "the portfolio and recent market data. "
    "Use the available tools to fetch portfolio status, evaluate risk and, if prudent, "
    "place mock orders. Always call `get_portfolio_status` before trading and avoid "
    "selling below the entry price whenever possible."
)




async def _stream_nudges(
    session: aiohttp.ClientSession, base_url: str
) -> AsyncIterator[dict]:
    """Yield ensemble nudge events from the MCP server."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/ensemble_nudge"
    headers = {"Accept": "text/event-stream"}

    while True:
        try:
            async with session.get(
                url, params={"after": cursor}, headers=headers
            ) as resp:
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
                    print(f"[EnsembleAgent] Nudge event: {evt}")
                    yield evt
        except Exception:
            await asyncio.sleep(1)


async def _stream_selected_pairs(
    session: aiohttp.ClientSession, base_url: str
) -> AsyncIterator[list[str]]:
    """Yield lists of user-selected trading pairs."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/selected_pairs"
    headers = {"Accept": "text/event-stream"}

    while True:
        try:
            async with session.get(
                url, params={"after": cursor}, headers=headers
            ) as resp:
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
                    symbols = evt.get("symbols")
                    if isinstance(symbols, list):
                        print(f"[EnsembleAgent] Selected pairs updated: {symbols}")
                        yield symbols
        except Exception:
            await asyncio.sleep(1)


async def _stream_agent_prompts(
    session: aiohttp.ClientSession, base_url: str
) -> AsyncIterator[str]:
    """Yield prompt messages targeted at the ensemble agent."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/agent_prompt"
    headers = {"Accept": "text/event-stream"}

    while True:
        try:
            async with session.get(
                url, params={"after": cursor}, headers=headers
            ) as resp:
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
                    if evt.get("target") == "ensemble":
                        msg = evt.get("message")
                        if isinstance(msg, str):
                            print(f"[EnsembleAgent] Prompt received: {msg}")
                            yield msg
        except Exception:
            await asyncio.sleep(1)


async def _ensure_schedule(client: Client) -> None:
    """Create the ensemble nudge schedule if it doesn't exist."""
    sched_id = "ensemble-nudge-schedule"
    handle = client.get_schedule_handle(sched_id)
    try:
        await handle.describe()
        print("[EnsembleAgent] Nudge schedule already exists")
        return
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            EnsembleNudgeWorkflow.run,
            id=f"nudge-{secrets.token_hex(4)}",
            task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=30))]
        ),
    )
    await client.create_schedule(sched_id, schedule)
    print("[EnsembleAgent] Nudge schedule created")


async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the ensemble agent and react to strategy signals."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp"

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(address, namespace=namespace)
    schedule_started = False
    try:
        await client.get_schedule_handle("ensemble-nudge-schedule").describe()
        schedule_started = True
        print("[EnsembleAgent] Found existing nudge schedule")
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise

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
                tools = [t for t in tools_resp.tools if t.name in ALLOWED_TOOLS]
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
                print(
                    "[EnsembleAgent] Connected to MCP server with tools:",
                    [t.name for t in tools],
                )

                pairs_ref = {"pairs": []}

                async def consume_pairs() -> None:
                    nonlocal schedule_started
                    async for symbols in _stream_selected_pairs(http_session, base_url):
                        pairs_ref["pairs"] = symbols
                        print(f"[EnsembleAgent] New trading pairs: {symbols}")
                        if not schedule_started:
                            await _ensure_schedule(client)
                            schedule_started = True

                async def consume_prompts() -> None:
                    async for msg in _stream_agent_prompts(http_session, base_url):
                        conversation.append({"role": "user", "content": msg})

                pairs_task = asyncio.create_task(consume_pairs())
                prompt_task = asyncio.create_task(consume_prompts())
                try:

                    async for _ in _stream_nudges(http_session, base_url):
                        pair_str = (
                            ", ".join(pairs_ref["pairs"])
                            if pairs_ref["pairs"]
                            else "none"
                        )
                        signal_str = (
                            f"Nudge received. Pairs in focus: {pair_str}. "
                            "Use your tools to review the portfolio and market "
                            "before deciding whether to trade."
                        )
                        print(f"[EnsembleAgent] Prompting LLM: {signal_str}")
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
                            try:
                                response = openai_client.chat.completions.create(
                                    model=os.environ.get("OPENAI_MODEL", "o4-mini"),
                                    messages=conversation,
                                    tools=openai_tools,
                                    tool_choice="auto",
                                )
                            except Exception as exc:
                                print(f"[EnsembleAgent] LLM request failed: {exc}")
                                await asyncio.sleep(5)
                                continue
                            msg = response.choices[0].message
                            print(
                                f"[EnsembleAgent] LLM raw response: {msg.model_dump() if hasattr(msg, 'model_dump') else msg}"
                            )

                        # Newer versions of the OpenAI SDK return a ChatCompletionMessage
                        # object. Inspect its attributes instead of treating it like a
                        # dictionary.
                        if getattr(msg, "tool_calls", None):
                            conversation.append(
                                {
                                    "role": msg.role,
                                    "content": msg.content,
                                    "tool_calls": [
                                        tc.model_dump() for tc in msg.tool_calls
                                    ],
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
                                print(
                                    f"{ORANGE}[EnsembleAgent] Tool result from {func_name}: {_tool_result_data(result)}{RESET}"
                                )
                                conversation.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "name": func_name,
                                        "content": json.dumps(
                                            _tool_result_data(result)
                                        ),
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
                            print(
                                f"{ORANGE}[EnsembleAgent] Tool result from {func_name}: {_tool_result_data(result)}{RESET}"
                            )
                            conversation.append(
                                {
                                    "role": "function",
                                    "name": func_name,
                                    "content": json.dumps(
                                        _tool_result_data(result)
                                    ),
                                }
                            )
                            continue

                        assistant_reply = msg.content or ""
                        conversation.append(
                            {"role": "assistant", "content": assistant_reply}
                        )
                        print(
                            f"{PINK}[EnsembleAgent] Decision: {assistant_reply}{RESET}"
                        )
                        # keep the conversation going across nudges so the
                        # assistant maintains context of past trades and
                        # observations
                        break
                finally:
                    pairs_task.cancel()
                    prompt_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await pairs_task
                    with suppress(asyncio.CancelledError):
                        await prompt_task


if __name__ == "__main__":
    asyncio.run(
        run_ensemble_agent(os.environ.get("MCP_SERVER", "http://localhost:8080"))
    )
