import os
import json
import asyncio
from typing import Any, AsyncIterator, Set
from datetime import timedelta
import aiohttp
import openai
import logging
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from agents.utils import stream_chat_completion


from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
    ScheduleIntervalSpec,
    RPCError,
    RPCStatusCode,
)
from tools.ensemble_nudge import EnsembleNudgeWorkflow

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

# Tools this agent is allowed to call
ALLOWED_TOOLS = {
    "place_mock_order",
    "get_historical_ticks",
    "get_portfolio_status",
}

# Maximum number of messages to keep in the conversation history. The
# broker agent applies the same limit to avoid exceeding the LLM context
# window.
MAX_CONVERSATION_LENGTH = 20

NUDGE_SCHEDULE_ID = "ensemble-nudge"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a portfolio management agent that wakes every minute when nudged. "
    "Each nudge message lists the active symbols. At the start of every nudge, "
    "call `get_historical_ticks` once with **all** of those symbols and include a "
    "`since_ts` parameter so you only fetch new data. Immediately afterward, call "
    "`get_portfolio_status` once to review cash balances and open positions. Do not "
    "repeat these two calls again until the next nudge. After reviewing this "
    "information you may decide whether to place a trade using `place_mock_order`, "
    "which must be invoked with a single `intent` object containing `symbol`, "
    "`side` (BUY or SELL), `qty`, `price`, and `type` (market or limit). Briefly "
    "explain your reasoning whenever you trade and always consider every active "
    "symbol before making a decision."
)


async def _watch_symbols(
    session: aiohttp.ClientSession,
    base_url: str,
    symbols: Set[str],
) -> None:
    """Update ``symbols`` whenever the broker agent selects pairs."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/active_symbols"
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
                    syms = evt.get("symbols")
                    if ts is None or not isinstance(syms, list):
                        continue
                    cursor = max(cursor, ts)
                    new_set = set(syms)
                    if new_set != symbols:
                        symbols.clear()
                        symbols.update(new_set)
                        print(
                            f"[EnsembleAgent] Active symbols updated: {sorted(symbols)}"
                        )
        except Exception:
            await asyncio.sleep(1)


async def _stream_nudges(
    session: aiohttp.ClientSession, base_url: str
) -> AsyncIterator[int]:
    """Yield timestamps from ensemble nudge events."""
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
                    if ts is None:
                        continue
                    cursor = max(cursor, ts)
                    yield ts
        except Exception:
            await asyncio.sleep(1)


async def _ensure_schedule(client: Client) -> None:
    """Create the nudge schedule if it doesn't already exist."""
    handle = client.get_schedule_handle(NUDGE_SCHEDULE_ID)
    try:
        await handle.describe()
        return
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            workflow=EnsembleNudgeWorkflow.run,
            args=[],
            id="ensemble-nudge-wf",
            task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=1))]),
    )
    await client.create_schedule(NUDGE_SCHEDULE_ID, schedule)


async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the ensemble agent and act on scheduled nudges."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp/"

    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        temporal = await Client.connect(
            os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
            namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        )
        symbols: Set[str] = set()
        _symbol_task = asyncio.create_task(
            _watch_symbols(http_session, base_url, symbols)
        )
        await _ensure_schedule(temporal)

        async with streamablehttp_client(mcp_url) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                all_tools = tools_resp.tools
                tools = [t for t in all_tools if t.name in ALLOWED_TOOLS]
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
                print(
                    "[EnsembleAgent] Connected to MCP server with tools:",
                    [t.name for t in tools],
                )

                async for ts in _stream_nudges(http_session, base_url):
                    if not symbols:
                        continue
                    print(f"[EnsembleAgent] Nudge @ {ts} for {sorted(symbols)}")
                    conversation.append(
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "nudge": ts,
                                    "symbols": sorted(symbols),
                                }
                            ),
                        }
                    )
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
                        msg = stream_chat_completion(
                            openai_client,
                            model=os.environ.get("OPENAI_MODEL", "o4-mini"),
                            messages=conversation,
                            tools=openai_tools,
                            tool_choice="auto",
                            prefix="[EnsembleAgent] Decision: ",
                            color=PINK,
                            reset=RESET,
                        )

                        if msg.get("tool_calls"):
                            conversation.append(
                                {
                                    "role": msg.get("role", "assistant"),
                                    "content": msg.get("content"),
                                    "tool_calls": msg["tool_calls"],
                                }
                            )
                            for tool_call in msg["tool_calls"]:
                                func_name = tool_call["function"]["name"]
                                func_args = json.loads(
                                    tool_call["function"].get("arguments") or "{}"
                                )
                                if func_name not in ALLOWED_TOOLS:
                                    print(
                                        f"[EnsembleAgent] Tool not allowed: {func_name}"
                                    )
                                    continue
                                print(
                                    f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                                )
                                result = await session.call_tool(func_name, func_args)
                                conversation.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_call["id"],
                                        "name": func_name,
                                        "content": json.dumps(result.model_dump()),
                                    }
                                )
                            continue

                        if msg.get("function_call"):
                            conversation.append(
                                {
                                    "role": msg.get("role", "assistant"),
                                    "content": msg.get("content"),
                                    "function_call": msg["function_call"],
                                }
                            )
                            func_name = msg["function_call"].get("name")
                            func_args = json.loads(
                                msg["function_call"].get("arguments") or "{}"
                            )
                            if func_name not in ALLOWED_TOOLS:
                                print(f"[EnsembleAgent] Tool not allowed: {func_name}")
                                continue
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

                        assistant_reply = msg.get("content", "")
                        conversation.append(
                            {"role": "assistant", "content": assistant_reply}
                        )
                        break

                    if len(conversation) > MAX_CONVERSATION_LENGTH:
                        conversation = [conversation[0]] + conversation[
                            -(MAX_CONVERSATION_LENGTH - 1) :
                        ]


if __name__ == "__main__":
    asyncio.run(
        run_ensemble_agent(os.environ.get("MCP_SERVER", "http://localhost:8080"))
    )
