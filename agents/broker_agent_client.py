import os
import json
import asyncio
import logging

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"
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

EXCHANGE = "coinbaseexchange"

LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)



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
        return [c.model_dump() if hasattr(c, "model_dump") else c for c in result.content]
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
            tools = (await session.list_tools()).tools
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print(
                f"[BrokerAgent] Connected to MCP server with tools: {[t.name for t in tools]}"
            )

            print(f"[BrokerAgent] Welcome to {EXCHANGE}!")

            if _openai_client is not None:
                try:
                    response = _openai_client.chat.completions.create(
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                    )
                    msg = response.choices[0].message
                    msg_dict = msg if isinstance(msg, dict) else msg.model_dump()
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    print(f"{PINK}[BrokerAgent] {assistant_msg}{RESET}")
                except Exception as exc:
                    logger.error("LLM request failed: %s", exc)
            else:
                print("[BrokerAgent] Please specify trading pairs like BTC/USD")

            print("[BrokerAgent] Type trade commands like 'buy 1 BTC/USD' or 'status'. 'quit' exits.")

            while True:
                user_request = await get_next_broker_command()
                if user_request is None:
                    await asyncio.sleep(1)
                    continue

                if user_request.strip().lower() in {"quit", "exit"}:
                    break
                if user_request.strip().lower() == "status":
                    try:
                        result = await session.call_tool("get_portfolio_status", {})
                        status = _tool_result_data(result)
                        cash = status.get("cash")
                        pnl = status.get("pnl")
                        positions = status.get("positions", {})
                        logger.info(
                            "Cash: %.2f, P&L: %.2f, Positions: %s",
                            cash,
                            pnl,
                            json.dumps(positions),
                        )
                    except Exception as exc:
                        logger.error("Failed to fetch status: %s", exc)
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
                    response = _openai_client.chat.completions.create(
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                        tools=tools_payload,
                        tool_choice="auto",
                    )
                    msg = response.choices[0].message
                    msg_dict = msg if isinstance(msg, dict) else msg.model_dump()
                except Exception as exc:
                    logger.error("LLM request failed: %s", exc)
                    continue

                if "tool_calls" in msg_dict and msg_dict.get("tool_calls"):
                    conversation.append(msg_dict)
                    for call in msg_dict.get("tool_calls", []):
                        func_name = call["function"]["name"]
                        func_args = json.loads(call["function"].get("arguments") or "{}")
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
                        followup = _openai_client.chat.completions.create(
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                            messages=conversation,
                            tools=tools_payload,
                        )
                        assistant_msg = followup.choices[0].message.content or ""
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
                    print(f"{ORANGE}[BrokerAgent] Tool requested: {func_name} {func_args}{RESET}")
                    try:
                        result = await session.call_tool(func_name, func_args)
                        result_data = _tool_result_data(result)
                    except Exception as exc:
                        logger.error("Tool call failed: %s", exc)
                        continue
                    conversation.append({"role": "function", "name": func_name, "content": json.dumps(result_data)})
                    try:
                        followup = _openai_client.chat.completions.create(
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                            messages=conversation,
                            tools=tools_payload,
                        )
                        assistant_msg = followup.choices[0].message.content or ""
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
