import os
import json
import asyncio
import logging
from typing import Any

from mcp.types import CallToolResult, TextContent
try:
    import openai
    _openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception:  # pragma: no cover - optional dependency
    openai = None
    _openai_client = None
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from .utils import stream_chat_completion

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

# Tools this agent is allowed to call
ALLOWED_TOOLS = {
    "start_market_stream",
    "get_historical_ticks",
    "get_portfolio_status",
}

EXCHANGE = "coinbaseexchange"

LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)



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


SYSTEM_PROMPT = (
    "You are a trading broker agent operating on the Coinbase exchange. "
    "Ask the user which of these trading pairs they want to trade: "
    "BTC/USD, ETH/USD, DOGE/USD, LTC/USD, ADA/USD, SOL/USD, DOT/USD. "
    "Only trade pairs you know are available on Coinbase. "
    "You manage execution of trades and the account state. "
    "When asked to execute a trade or query account status, use the appropriate tool and report the result. "
    "As soon as the user confirms their desired pairs, automatically begin streaming market data for them via the `start_market_stream` tool."
)

async def get_next_broker_command() -> str | None:
    return await asyncio.to_thread(input, "> ")

async def run_broker_agent(server_url: str = "http://localhost:8080"):
    url = server_url.rstrip("/") + "/mcp/"
    logger.info("Connecting to MCP server at %s", url)
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            all_tools = (await session.list_tools()).tools
            tools = [t for t in all_tools if t.name in ALLOWED_TOOLS]
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print(
                f"[BrokerAgent] Connected to MCP server with tools: {[t.name for t in tools]}"
            )

            if _openai_client is not None:
                try:
                    msg_dict = stream_chat_completion(
                        _openai_client,
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                    )
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    print(f"{PINK}[BrokerAgent] {assistant_msg}{RESET}")
                except Exception as exc:
                    logger.error("LLM request failed: %s", exc)
            else:
                print("[BrokerAgent] Please specify trading pairs like BTC/USD")

            while True:
                user_request = await get_next_broker_command()
                if user_request is None:
                    await asyncio.sleep(1)
                    continue

                if _openai_client is None:
                    logger.warning("LLM unavailable; echoing command.")
                    continue

                logger.info("User command: %s", user_request)
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
                        _openai_client,
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                        tools=tools_payload,
                        tool_choice="auto",
                    )
                except Exception as exc:
                    logger.error("LLM request failed: %s", exc)
                    continue

                if "tool_calls" in msg_dict and msg_dict.get("tool_calls"):
                    conversation.append(msg_dict)
                    for call in msg_dict.get("tool_calls", []):
                        func_name = call["function"]["name"]
                        func_args = json.loads(call["function"].get("arguments") or "{}")
                        if func_name not in ALLOWED_TOOLS:
                            logger.warning("Tool not allowed: %s", func_name)
                            continue
                        print(f"{ORANGE}[BrokerAgent] Tool requested: {func_name} {func_args}{RESET}")
                        try:
                            result = await session.call_tool(func_name, func_args)
                            result_data = _tool_result_data(result)
                        except Exception as exc:
                            logger.error("Tool call failed: %s", exc)
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
                            _openai_client,
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                            messages=conversation,
                            tools=tools_payload,
                        )
                        assistant_msg = followup.get("content", "")
                        conversation.append({"role": "assistant", "content": assistant_msg})
                        print(f"{PINK}[BrokerAgent] {assistant_msg}{RESET}")
                    except Exception as exc:
                        logger.error("LLM request failed: %s", exc)
                        continue
                elif msg_dict.get("function_call"):
                    conversation.append(msg_dict)
                    function_call = msg_dict["function_call"]
                    func_name = function_call.get("name")
                    if not func_name:
                        logger.error("Received function_call without name: %s", msg_dict)
                        continue
                    func_args = json.loads(function_call.get("arguments") or "{}")
                    if func_name not in ALLOWED_TOOLS:
                        logger.warning("Tool not allowed: %s", func_name)
                        continue
                    print(f"{ORANGE}[BrokerAgent] Tool requested: {func_name} {func_args}{RESET}")
                    try:
                        result = await session.call_tool(func_name, func_args)
                        result_data = _tool_result_data(result)
                    except Exception as exc:
                        logger.error("Tool call failed: %s", exc)
                        continue
                    conversation.append({"role": "function", "name": func_name, "content": json.dumps(result_data)})
                    try:
                        followup = stream_chat_completion(
                            _openai_client,
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                            messages=conversation,
                            tools=tools_payload,
                        )
                        assistant_msg = followup.get("content", "")
                        conversation.append({"role": "assistant", "content": assistant_msg})
                        print(f"{PINK}[BrokerAgent] {assistant_msg}{RESET}")
                    except Exception as exc:
                        logger.error("LLM request failed: %s", exc)
                        continue
                else:
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    print(f"{PINK}[BrokerAgent] {assistant_msg}{RESET}")

                if len(conversation) > 20:
                    conversation = [conversation[0]] + conversation[-19:]

if __name__ == "__main__":
    asyncio.run(run_broker_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
