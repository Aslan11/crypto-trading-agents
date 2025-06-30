import os
import json
import asyncio
from typing import Any, AsyncIterator
import aiohttp
import openai

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent
import time

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a strategy ensemble agent. You take note of the current status of the portfolio, "
    "aggregate trading signals from multiple strategies, "
    "perform risk checks, and autonomously execute trades that pass those checks. "
    "You have tools for fetching the current status of the portfolio, risk assessment, "
    "broadcasting intents, and placing mock orders. Always call these tools yourself."
    "Before approving or rejecting any intent, always call `get_portfolio_status` to "
    "review cash balances, open positions and entry prices. Use this information to "
    "validate whether a BUY or SELL makes sense. Do your best to avoid selling below "
    "the entry price whenever possible."
    "Before deciding, also call `get_historical_performance` to understand recent price trends. "
    "Once `pre_trade_risk_check` approves an intent, "
    "decide whether or not it makes sense to execute it via `place_mock_order` "
    "without waiting for human confirmation, then briefly explain your decision & the outcome."
)


def _tool_result_data(result: Any) -> Any:
    """Return JSON-friendly data from a tool call result."""
    if isinstance(result, CallToolResult):
        if result.content:
            first = result.content[0]
            if isinstance(first, TextContent):
                try:
                    return json.loads(first.text)
                except Exception:
                    return first.text
        return [
            c.model_dump() if hasattr(c, "model_dump") else c
            for c in result.content
        ]
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

async def _stream_strategy_signals(
    session: aiohttp.ClientSession, base_url: str
) -> AsyncIterator[dict]:
    """Yield strategy signals from the MCP server."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/strategy_signal"
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

async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the ensemble agent and react to strategy signals."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp"

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

                async for incoming_signal in _stream_strategy_signals(
                    http_session, base_url
                ):
                    intent = {
                        "symbol": incoming_signal.get("symbol"),
                        "side": incoming_signal.get("side"),
                        "qty": 1.0,
                        "ts": incoming_signal.get("ts"),
                    }
                    price = await _latest_price(http_session, base_url, intent["symbol"])
                    intent["price"] = price
                    status_result = await session.call_tool("get_portfolio_status", {})
                    status = _tool_result_data(status_result)
                    positions = status.get("positions", {}) if isinstance(status, dict) else {}
                    cash = status.get("cash", 0.0) if isinstance(status, dict) else 0.0
                    if (
                        intent["side"] == "SELL"
                        and positions.get(intent["symbol"], 0.0) <= 0.0
                    ):
                        print("[EnsembleAgent] Skipping SELL - no position")
                        continue
                    if intent["side"] == "BUY" and cash < price * intent["qty"]:
                        print("[EnsembleAgent] Skipping BUY - insufficient cash")
                        continue
                    intent_id = f"{intent['side']}-{intent['symbol']}-{intent['ts']}"
                    signal_str = (
                        f"Strategy signal received: {json.dumps(incoming_signal)}. "
                        f"Current portfolio: {json.dumps(status)}. "
                        f"Latest price: {price}. Decide whether to approve this trade intent."
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
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
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
                                if func_name == "pre_trade_risk_check" and "intents" not in func_args:
                                    func_args.setdefault("intent_id", intent_id)
                                    func_args["intents"] = [intent]
                                if func_name == "place_mock_order" and "intent" not in func_args:
                                    func_args["intent"] = intent
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
                            if func_name == "pre_trade_risk_check" and "intents" not in func_args:
                                func_args.setdefault("intent_id", intent_id)
                                func_args["intents"] = [intent]
                            if func_name == "place_mock_order" and "intent" not in func_args:
                                func_args["intent"] = intent
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
