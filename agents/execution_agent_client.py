import os
import json
import asyncio
from typing import Any, AsyncIterator, Set
import aiohttp
import openai
import logging
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from agents.utils import stream_chat_completion
from mcp.types import CallToolResult, TextContent
from datetime import timedelta
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
    "You are a portfolio‑management agent in a proactive AI system that wakes only when nudged. "
    "You have a moderate risk profile and focus on short‑horizon scalping. "
    "You have complete agency and will not interact with humans, but you must obey EVERY rule below.\n\n"

    # ───────────────────────────────  GLOBAL NUDGE LIFECYCLE  ───────────────────────────────
    "**Exactly one `get_historical_ticks` per nudge**\n"
    "   • Maintain an internal Boolean flag `called_ticks` (initially False each nudge).\n"
    "   • When the nudge arrives, first set `called_ticks` to False.\n"
    "   • Immediately call `get_historical_ticks` with:\n"
    "       ▸ `symbols`: ALL followed tickers (no omissions)\n"
    "       ▸ `since_ts`: the latest timestamp already processed (0 on first use)\n"
    "   • After this call, set `called_ticks = True` and **never call it again** "
    "until the next nudge. If logic ever tries a second call while `called_ticks` is True, "
    "abort the call and treat it as a fatal logic error.\n\n"

    "**Exactly one `get_portfolio_status` per nudge**, called *after* the single "
    "`get_historical_ticks` call.\n\n"

    # ───────────────────────────────  PER‑SYMBOL ANALYSIS  ───────────────────────────────
    "For every symbol returned:\n"
    "   • Combine new ticks with current positions & cash.\n"
    "   • Decide: BUY, SELL, or HOLD.\n"
    "   • Record a short rationale (even for HOLD).\n\n"

    # ───────────────────────────────  SAFETY CHECKS  ───────────────────────────────
    "Before placing an order:\n"
    "   • BUY → ensure cash ≥ qty × price.\n"
    "   • SELL → ensure position qty ≥ desired qty.\n"
    "   • If either check fails, downgrade decision to HOLD.\n\n"

    # ───────────────────────────────  ORDER EXECUTION  ───────────────────────────────
    "Only when the **final** decision is BUY or SELL, call `place_mock_order` **once per trade** "
    "with exactly the following payload (note the surrounding **`intent`** key):\n"
    "      `{"
    "\"intent\": {"
    "\"symbol\": <str>, "
    "\"side\": \"BUY\" | \"SELL\", "
    "\"qty\": <number>, "
    "\"price\": <number>, "
    "\"type\": \"market\" | \"limit\""
    "}}`\n"
    "   • Never pass 'HOLD' as the `side`.\n"
    "   • Never call `place_mock_order` for HOLD decisions.\n\n"

    # ───────────────────────────────  STATE PERSISTENCE  ───────────────────────────────
    "After processing, store the newest tick timestamp for next nudge's `since_ts`.\n\n"

    # ───────────────────────────────  REPORTING  ───────────────────────────────
    "Output a structured report listing every symbol with its decision & rationale, "
    "then list any order intents submitted.\n\n"

    "All rules are mandatory. Skipping, reordering, or violating any rule constitutes a fatal error."
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
                            f"[ExecutionAgent] Active symbols updated: {sorted(symbols)}"
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


async def run_execution_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the execution agent and act on scheduled nudges."""
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
                    "[ExecutionAgent] Connected to MCP server with tools:",
                    [t.name for t in tools],
                )

                async for ts in _stream_nudges(http_session, base_url):
                    if not symbols:
                        continue
                    print(f"[ExecutionAgent] Nudge @ {ts} for {sorted(symbols)}")
                    conversation.append(
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"nudge": ts, "symbols": sorted(symbols)}
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
                        try:
                            msg = stream_chat_completion(
                                openai_client,
                                model=os.environ.get("OPENAI_MODEL", "o4-mini"),
                                messages=conversation,
                                tools=openai_tools,
                                tool_choice="auto",
                                prefix="[ExecutionAgent] Decision: ",
                                color=PINK,
                                reset=RESET,
                            )
                        except openai.OpenAIError as exc:
                            print(f"[ExecutionAgent] LLM request failed: {exc}")
                            conversation = [conversation[0], conversation[-1]]
                            break

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
                                        f"[ExecutionAgent] Tool not allowed: {func_name}"
                                    )
                                    continue
                                print(
                                    f"{ORANGE}[ExecutionAgent] Tool requested: {func_name} {func_args}{RESET}"
                                )
                                result = await session.call_tool(func_name, func_args)
                                conversation.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_call["id"],
                                        "name": func_name,
                                        "content": json.dumps(_tool_result_data(result)),
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
                                print(f"[ExecutionAgent] Tool not allowed: {func_name}")
                                continue
                            print(
                                f"{ORANGE}[ExecutionAgent] Tool requested: {func_name} {func_args}{RESET}"
                            )
                            result = await session.call_tool(func_name, func_args)
                            conversation.append(
                                {
                                    "role": "function",
                                    "name": func_name,
                                    "content": json.dumps(_tool_result_data(result)),
                                }
                            )
                            continue

                        assistant_reply = msg.get("content", "")
                        conversation.append(
                            {"role": "assistant", "content": assistant_reply}
                        )
                        break

                    if len(conversation) > MAX_CONVERSATION_LENGTH:
                        conversation = [conversation[0]] + conversation[-(MAX_CONVERSATION_LENGTH - 1):]



if __name__ == "__main__":
    asyncio.run(
        run_execution_agent(os.environ.get("MCP_SERVER", "http://localhost:8080"))
    )
