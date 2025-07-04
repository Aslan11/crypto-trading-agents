import os
import json
import asyncio
from datetime import timedelta
from typing import Any, AsyncIterator, Dict
import aiohttp
import openai
from temporalio.client import Client, RPCError, RPCStatusCode, Schedule, ScheduleSpec, ScheduleIntervalSpec, ScheduleActionStartWorkflow
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent
from agents.utils import stream_chat_completion
from tools.ensemble_nudge import SendEnsembleNudge

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

ALLOWED_TOOLS = {
    "place_mock_order",
    "get_historical_ticks",
    "get_portfolio_status",
}

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are an autonomous trading agent awakened every 30 seconds by a scheduler. "
    "Use your tools to fetch historical price data and the current portfolio status "
    "for all active trading pairs. Analyze the information provided by get_historical_ticks "
    "and decide whether to place buy or sell orders via `place_mock_order`. Always check "
     "`get_portfolio_status` before trading and avoid selling below the entry price"
    " when possible. Respond with a brief explanation of each decision."
)


def _tool_result_data(result: Any) -> Any:
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


async def _stream_events(
    session: aiohttp.ClientSession, url: str
) -> AsyncIterator[dict]:
    cursor = 0
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
    handle = client.get_schedule_handle("ensemble-nudge")
    try:
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            spec = ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=30))])
            action = ScheduleActionStartWorkflow(
                SendEnsembleNudge.run,
                id="nudge-ensemble",
                task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
            )
            await client.create_schedule("ensemble-nudge", Schedule(spec=spec, action=action))
            print("[EnsembleAgent] Created schedule ensemble-nudge")
        else:
            raise


async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp/"
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with streamablehttp_client(mcp_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                tools = [t for t in tools_resp.tools if t.name in ALLOWED_TOOLS]
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
                print(
                    "[EnsembleAgent] Connected to MCP server with tools:",
                    [t.name for t in tools],
                )

                temporal = await Client.connect(
                    os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
                    namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
                )
                await _ensure_schedule(temporal)

                active_symbols: set[str] = set()

                async def watch_ticks() -> None:
                    url = base_url + "/signal/market_tick"
                    async for tick in _stream_events(http_session, url):
                        sym = tick.get("symbol")
                        if sym:
                            if sym not in active_symbols:
                                print(f"[EnsembleAgent] Tracking pair {sym}")
                            active_symbols.add(sym)

                async def process_nudges() -> None:
                    nonlocal conversation
                    url = base_url + "/signal/ensemble_nudge"
                    async for _ in _stream_events(http_session, url):
                        if not active_symbols:
                            continue
                        print("[EnsembleAgent] Nudge received")
                        portfolio_res = await session.call_tool("get_portfolio_status", {})
                        portfolio = _tool_result_data(portfolio_res)
                        history: Dict[str, Any] = {}
                        for sym in sorted(active_symbols):
                            h = await session.call_tool("get_historical_ticks", {"symbol": sym})
                            history[sym] = _tool_result_data(h)
                        user_msg = json.dumps({"portfolio": portfolio, "history": history, "symbols": sorted(active_symbols)})
                        conversation.append({"role": "user", "content": user_msg})
                        openai_tools = [
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
                                for call in msg["tool_calls"]:
                                    func_name = call["function"]["name"]
                                    func_args = json.loads(call["function"].get("arguments") or "{}")
                                    if func_name not in ALLOWED_TOOLS:
                                        print(f"[EnsembleAgent] Tool not allowed: {func_name}")
                                        continue
                                    print(f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}")
                                    result = await session.call_tool(func_name, func_args)
                                    conversation.append(
                                        {
                                            "role": "tool",
                                            "tool_call_id": call.get("id"),
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
                                func_args = json.loads(msg["function_call"].get("arguments") or "{}")
                                if func_name not in ALLOWED_TOOLS:
                                    print(f"[EnsembleAgent] Tool not allowed: {func_name}")
                                    continue
                                print(f"{ORANGE}[EnsembleAgent] Tool requested: {func_name} {func_args}{RESET}")
                                result = await session.call_tool(func_name, func_args)
                                conversation.append(
                                    {
                                        "role": "function",
                                        "name": func_name,
                                        "content": json.dumps(result.model_dump()),
                                    }
                                )
                                continue

                            conversation.append({"role": "assistant", "content": msg.get("content", "")})
                            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
                            break

                await asyncio.gather(watch_ticks(), process_nudges())


if __name__ == "__main__":
    asyncio.run(run_ensemble_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
