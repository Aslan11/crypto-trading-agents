import os
import json
import asyncio
from typing import AsyncIterator
import aiohttp
import openai
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a strategy ensemble agent. You aggregate trading signals from multiple strategies, "
    "perform risk checks, and autonomously execute trades that pass those checks. "
    "You have tools for risk assessment, broadcasting intents, and placing mock orders. "
    "Always call these tools yourself. Once `pre_trade_risk_check` approves an intent, "
    "immediately execute it via `place_mock_order` without waiting for human confirmation, "
    "then briefly explain the outcome."
)

async def _stream_strategy_signals(
    session: aiohttp.ClientSession, base_url: str
) -> AsyncIterator[dict]:
    """Yield strategy signals from the MCP server."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/strategy_signal"
    while True:
        try:
            async with session.get(url, params={"after": cursor}) as resp:
                if resp.status == 200:
                    events = await resp.json()
                else:
                    events = []
        except Exception:
            events = []

        if not events:
            await asyncio.sleep(1)
            continue

        for evt in events:
            ts = evt.get("ts")
            if ts is not None:
                cursor = max(cursor, ts)
            yield evt

async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the ensemble agent and react to strategy signals."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp"

    timeout = aiohttp.ClientTimeout(total=30)
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
                        "price": incoming_signal.get("price", 0.0),
                        "ts": incoming_signal.get("ts"),
                    }
                    intent_id = f"{intent['side']}-{intent['symbol']}-{intent['ts']}"
                    signal_str = (
                        f"Strategy signal received: {json.dumps(incoming_signal)}. "
                        f"Decide whether to approve this trade intent."
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
                                    f"[EnsembleAgent] Tool requested: {func_name} {func_args}"
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
                                f"[EnsembleAgent] Tool requested: {func_name} {func_args}"
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
                        print(f"[EnsembleAgent] Decision: {assistant_reply}")
                        conversation = [
                            {"role": "system", "content": SYSTEM_PROMPT}
                        ]
                        break

if __name__ == "__main__":
    asyncio.run(run_ensemble_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
