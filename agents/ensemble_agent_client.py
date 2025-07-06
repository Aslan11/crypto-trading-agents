import os
import json
import asyncio
from typing import Any, AsyncIterator, Set, Dict
import aiohttp
import openai
import logging
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from agents.utils import stream_chat_completion
from mcp.types import CallToolResult, TextContent
from datetime import datetime, timedelta, timezone
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
# Maximum historical lookback in days for the initial tick request
TICK_LOOKBACK_DAYS = int(os.environ.get("TICK_LOOKBACK_DAYS", "1"))

NUDGE_SCHEDULE_ID = "ensemble-nudge"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a portfolio management agent that wakes every minute when nudged. "
    "On each and every nudge you must first call `get_historical_ticks` once with all "
    "active symbols, followed immediately by `get_portfolio_status` to review cash "
    "balances and open positions. These two tools must be invoked before making any "
    "trading decision. If you decide to trade, call `place_mock_order` with an intent "
    "containing `symbol`, `side` (BUY or SELL), `qty`, `price` and `type` (market or "
    "limit). Include the `since_ts` parameter when requesting historical ticks so you "
    "only fetch new data. Briefly explain your reasoning whenever you execute a trade."
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
                    parsed.append(
                        item.model_dump() if hasattr(item, "model_dump") else item
                    )
            return parsed if len(parsed) > 1 else parsed[0]
        return []
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result


async def get_next_ensemble_command() -> str | None:
    """Return the next user command from stdin."""
    return await asyncio.to_thread(input, "> ")


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


async def _interactive_chat(
    session: ClientSession,
    tools: list,
    conversation: list[dict],
) -> None:
    """Prompt the user for follow-ups until they enter an empty command."""
    if openai_client is None:
        return

    tools_payload = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.inputSchema,
            },
        }
        for t in tools
    ]

    while True:
        user_request = await get_next_ensemble_command()
        if user_request is None:
            await asyncio.sleep(1)
            continue
        if not user_request.strip():
            break

        logging.info("User command: %s", user_request)
        conversation.append({"role": "user", "content": user_request})

        while True:
            try:
                msg_dict = stream_chat_completion(
                    openai_client,
                    model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                    messages=conversation,
                    tools=tools_payload,
                    tool_choice="auto",
                    prefix="[EnsembleAgent] ",
                    color=PINK,
                    reset=RESET,
                )
            except Exception as exc:
                logging.error("LLM request failed: %s", exc)
                break

            if "tool_calls" in msg_dict and msg_dict.get("tool_calls"):
                conversation.append(msg_dict)
                for call in msg_dict.get("tool_calls", []):
                    func_name = call["function"]["name"]
                    func_args = json.loads(call["function"].get("arguments") or "{}")
                    if func_name not in ALLOWED_TOOLS:
                        logging.warning("Tool not allowed: %s", func_name)
                        continue
                    print(
                        f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                    )
                    try:
                        result = await session.call_tool(func_name, func_args)
                        result_data = _tool_result_data(result)
                    except Exception as exc:
                        logging.error("Tool call failed: %s", exc)
                        continue
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": json.dumps(result_data),
                        }
                    )
                continue
            elif msg_dict.get("function_call"):
                conversation.append(msg_dict)
                function_call = msg_dict["function_call"]
                func_name = function_call.get("name")
                if not func_name:
                    logging.error("Received function_call without name: %s", msg_dict)
                    continue
                func_args = json.loads(function_call.get("arguments") or "{}")
                if func_name not in ALLOWED_TOOLS:
                    logging.warning("Tool not allowed: %s", func_name)
                    continue
                print(
                    f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                )
                try:
                    result = await session.call_tool(func_name, func_args)
                    result_data = _tool_result_data(result)
                except Exception as exc:
                    logging.error("Tool call failed: %s", exc)
                    continue
                conversation.append(
                    {
                        "role": "function",
                        "name": func_name,
                        "content": json.dumps(result_data),
                    }
                )
                continue
            else:
                assistant_msg = msg_dict.get("content", "")
                conversation.append({"role": "assistant", "content": assistant_msg})
                break

        if len(conversation) > MAX_CONVERSATION_LENGTH:
            conversation = [conversation[0]] + conversation[
                -(MAX_CONVERSATION_LENGTH - 1) :
            ]


async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the ensemble agent and act on scheduled nudges with optional chat."""
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
        # track the most recent tick timestamp sent for each symbol
        last_tick_ts: Dict[str, int] = {}
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

                chat_task = asyncio.create_task(
                    _interactive_chat(session, tools, conversation)
                )
                try:
                    async for ts in _stream_nudges(http_session, base_url):
                        if not symbols:
                            continue
                        print(f"[EnsembleAgent] Nudge @ {ts} for {sorted(symbols)}")
                        if last_tick_ts:
                            since_ts = min(last_tick_ts.values())
                        else:
                            since_ts = (
                                int(datetime.now(timezone.utc).timestamp())
                                - TICK_LOOKBACK_DAYS * 86400
                            )
                        res = await session.call_tool(
                            "get_historical_ticks",
                            {"symbols": sorted(symbols), "since_ts": since_ts},
                        )
                        history = _tool_result_data(res)
                        new_history: Dict[str, list] = {}
                        for sym, ticks in history.items():
                            last_ts = last_tick_ts.get(sym, 0)
                            valid_ticks = [t for t in ticks if t.get("ts", 0) > last_ts]
                            if valid_ticks:
                                new_history[sym] = valid_ticks
                            if ticks:
                                max_ts = max(t.get("ts", 0) for t in ticks)
                                last_tick_ts[sym] = max(last_ts, max_ts)
                        status_res = await session.call_tool("get_portfolio_status", {})
                        status = _tool_result_data(status_res)
                        info = {"portfolio": status, "history": new_history}
                        conversation.append(
                            {"role": "user", "content": json.dumps(info)}
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
                finally:
                    chat_task.cancel()
                    with contextlib.suppress(Exception):
                        await chat_task


async def run_ensemble_chat_agent(server_url: str = "http://localhost:8080") -> None:
    """Interact with the ensemble agent via the terminal."""
    url = server_url.rstrip("/") + "/mcp/"
    logging.info("Connecting to MCP server at %s", url)
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            all_tools = (await session.list_tools()).tools
            tools = [t for t in all_tools if t.name in ALLOWED_TOOLS]
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print(
                f"[EnsembleAgent] Connected to MCP server with tools: {[t.name for t in tools]}"
            )

            if openai_client is not None:
                try:
                    msg_dict = stream_chat_completion(
                        openai_client,
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                        prefix="[EnsembleAgent] ",
                        color=PINK,
                        reset=RESET,
                    )
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                except Exception as exc:
                    logging.error("LLM request failed: %s", exc)
            else:
                print("[EnsembleAgent] Enter a command to begin chatting")

            while True:
                user_request = await get_next_ensemble_command()
                if user_request is None:
                    await asyncio.sleep(1)
                    continue

                if openai_client is None:
                    logging.warning("LLM unavailable; echoing command.")
                    continue

                logging.info("User command: %s", user_request)
                conversation.append({"role": "user", "content": user_request})

                tools_payload = [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema,
                        },
                    }
                    for t in tools
                ]

                try:
                    msg_dict = stream_chat_completion(
                        openai_client,
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                        tools=tools_payload,
                        tool_choice="auto",
                        prefix="[EnsembleAgent] ",
                        color=PINK,
                        reset=RESET,
                    )
                except Exception as exc:
                    logging.error("LLM request failed: %s", exc)
                    continue

                if "tool_calls" in msg_dict and msg_dict.get("tool_calls"):
                    conversation.append(msg_dict)
                    for call in msg_dict.get("tool_calls", []):
                        func_name = call["function"]["name"]
                        func_args = json.loads(
                            call["function"].get("arguments") or "{}"
                        )
                        if func_name not in ALLOWED_TOOLS:
                            logging.warning("Tool not allowed: %s", func_name)
                            continue
                        print(
                            f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                        )
                        try:
                            result = await session.call_tool(func_name, func_args)
                            result_data = _tool_result_data(result)
                        except Exception as exc:
                            logging.error("Tool call failed: %s", exc)
                            continue
                        conversation.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "content": json.dumps(result_data),
                            }
                        )

                    try:
                        followup = stream_chat_completion(
                            openai_client,
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                            messages=conversation,
                            tools=tools_payload,
                            prefix="[EnsembleAgent] ",
                            color=PINK,
                            reset=RESET,
                        )
                        assistant_msg = followup.get("content", "")
                        conversation.append(
                            {"role": "assistant", "content": assistant_msg}
                        )
                    except Exception as exc:
                        logging.error("LLM request failed: %s", exc)
                        continue
                elif msg_dict.get("function_call"):
                    conversation.append(msg_dict)
                    function_call = msg_dict["function_call"]
                    func_name = function_call.get("name")
                    if not func_name:
                        logging.error(
                            "Received function_call without name: %s", msg_dict
                        )
                        continue
                    func_args = json.loads(function_call.get("arguments") or "{}")
                    if func_name not in ALLOWED_TOOLS:
                        logging.warning("Tool not allowed: %s", func_name)
                        continue
                    print(
                        f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}"
                    )
                    try:
                        result = await session.call_tool(func_name, func_args)
                        result_data = _tool_result_data(result)
                    except Exception as exc:
                        logging.error("Tool call failed: %s", exc)
                        continue
                    conversation.append(
                        {
                            "role": "function",
                            "name": func_name,
                            "content": json.dumps(result_data),
                        }
                    )
                    try:
                        followup = stream_chat_completion(
                            openai_client,
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                            messages=conversation,
                            tools=tools_payload,
                            prefix="[EnsembleAgent] ",
                            color=PINK,
                            reset=RESET,
                        )
                        assistant_msg = followup.get("content", "")
                        conversation.append(
                            {"role": "assistant", "content": assistant_msg}
                        )
                    except Exception as exc:
                        logging.error("LLM request failed: %s", exc)
                        continue
                else:
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})

                if len(conversation) > MAX_CONVERSATION_LENGTH:
                    conversation = [conversation[0]] + conversation[
                        -(MAX_CONVERSATION_LENGTH - 1) :
                    ]


if __name__ == "__main__":
    server = os.environ.get("MCP_SERVER", "http://localhost:8080")
    asyncio.run(run_ensemble_agent(server))
