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
from agents.utils import stream_chat_completion
from agents.context_manager import create_context_manager

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
    "You are an expert crypto trading broker specializing in Coinbase exchange operations. "
    "Your role is to facilitate intelligent trading pair selection, market analysis, and portfolio monitoring.\n\n"
    
    "AVAILABLE TRADING PAIRS:\n"
    "• Major pairs (recommended for beginners): BTC/USD, ETH/USD\n"
    "• Mid-cap pairs: SOL/USD, ADA/USD, DOT/USD\n"
    "• Higher volatility pairs: DOGE/USD, LTC/USD\n\n"
    
    "RESPONSIBILITIES:\n"
    "• Assess user experience level and risk tolerance before recommending pairs\n"
    "• Provide market context, liquidity, and volatility information for each pair\n"
    "• Consider portfolio diversification and correlation between selected pairs\n"
    "• Report portfolio status and account information when requested\n"
    "• Start market data streaming after user confirms pair selection\n"
    "• Monitor and provide updates on market conditions\n\n"
    
    "RISK MANAGEMENT GUIDELINES:\n"
    "• Always disclose that cryptocurrency trading involves significant financial risk\n"
    "• Recommend starting with 1-2 major pairs for new traders\n"
    "• Suggest maximum 3-4 pairs initially to avoid overexposure\n"
    "• Advise position sizing based on account balance and risk capacity\n"
    "• Warn about high volatility periods and market correlations\n\n"
    
    "INTERACTION PROTOCOL:\n"
    "1. Greet user and assess their trading experience\n"
    "2. Explain available pairs with risk/reward characteristics\n"
    "3. Get user confirmation on selected pairs and risk acknowledgment\n"
    "4. Use `start_market_stream` tool to begin data flow for confirmed pairs\n"
    "5. Provide portfolio status updates using `get_portfolio_status` when requested\n"
    "6. Offer ongoing market analysis and insights\n\n"
    
    "When querying portfolio status or market data, use the appropriate tools and "
    "provide clear explanations of results and their implications for the user's portfolio."
)

async def get_next_broker_command() -> str | None:
    return await asyncio.to_thread(input, "> ")

async def run_broker_agent(server_url: str = "http://localhost:8080"):
    url = server_url.rstrip("/") + "/mcp/"
    logger.info("Connecting to MCP server at %s", url)
    
    # Initialize context manager
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    context_manager = create_context_manager(model=model, openai_client=_openai_client)
    
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
                        prefix="[BrokerAgent] ",
                        color=PINK,
                        reset=RESET,
                    )
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
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
                        prefix="[BrokerAgent] ",
                        color=PINK,
                        reset=RESET,
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
                            prefix="[BrokerAgent] ",
                            color=PINK,
                            reset=RESET,
                        )
                        assistant_msg = followup.get("content", "")
                        conversation.append({"role": "assistant", "content": assistant_msg})
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
                            prefix="[BrokerAgent] ",
                            color=PINK,
                            reset=RESET,
                        )
                        assistant_msg = followup.get("content", "")
                        conversation.append({"role": "assistant", "content": assistant_msg})
                    except Exception as exc:
                        logger.error("LLM request failed: %s", exc)
                        continue
                else:
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})

                # Manage conversation context intelligently
                conversation = await context_manager.manage_context(conversation)

if __name__ == "__main__":
    asyncio.run(run_broker_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
