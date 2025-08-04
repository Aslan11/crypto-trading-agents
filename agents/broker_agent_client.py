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
    "set_user_preferences",
    "get_user_preferences",
    "trigger_performance_evaluation",
    "get_judge_evaluations",
    "get_prompt_history",
    "get_performance_metrics",
    "get_risk_metrics",
    "get_transaction_history",
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
    "• Large-cap altcoins: SOL/USD, ADA/USD, AVAX/USD, MATIC/USD, DOT/USD\n"
    "• DeFi tokens: UNI/USD, AAVE/USD, COMP/USD, MKR/USD, SUSHI/USD\n"
    "• Layer 2 & Scaling: MATIC/USD, LRC/USD, IMX/USD\n"
    "• Stablecoins & Yield: USDC/USD, DAI/USD (for comparison)\n"
    "• Meme/High volatility: DOGE/USD, SHIB/USD\n"
    "• Established altcoins: LTC/USD, BCH/USD, XLM/USD, ALGO/USD\n"
    "• Newer projects: APT/USD, ARB/USD, OP/USD, NEAR/USD\n\n"
    
    "RESPONSIBILITIES:\n"
    "• Assess user experience level and risk tolerance before recommending pairs\n"
    "• Provide market context, liquidity, and volatility information for each pair\n"
    "• Consider portfolio diversification and correlation between selected pairs\n"
    "• Report portfolio status and account information when requested\n"
    "• Start market data streaming after user confirms pair selection\n"
    "• Monitor and provide updates on market conditions\n"
    "• Trigger and report on execution agent performance evaluations\n"
    "• Provide access to trading history, performance metrics, and system insights\n\n"
    
    "RISK MANAGEMENT GUIDELINES:\n"
    "• Always disclose that cryptocurrency trading involves significant financial risk\n"
    "• Recommend starting with 1-2 major pairs for new traders\n"
    "• Suggest maximum 3-4 pairs initially to avoid overexposure\n"
    "• Advise position sizing based on account balance and risk capacity\n"
    "• Warn about high volatility periods and market correlations\n\n"
    
    "INTERACTION PROTOCOL:\n"
    "1. Greet user and assess their trading experience and risk tolerance\n"
    "2. Capture and store user preferences using `set_user_preferences` tool\n"
    "3. Explain available pairs with risk/reward characteristics based on their profile\n"
    "4. Get user confirmation on selected pairs and risk acknowledgment\n"
    "5. Use `start_market_stream` tool to begin data flow for confirmed pairs\n"
    "6. Provide portfolio status updates using `get_portfolio_status` when requested\n"
    "7. Offer ongoing market analysis and insights\n\n"
    
    "USER PREFERENCE ASSESSMENT:\n"
    "CRITICAL: Follow this exact workflow based on user experience level:\n\n"
    "FOR BEGINNERS:\n"
    "1. Ask: Experience Level + Risk Tolerance only\n"
    "2. Immediately call `set_user_preferences` with just these two preferences\n"
    "3. Use sensible defaults for other parameters to keep it simple\n\n"
    "FOR INTERMEDIATE & ADVANCED:\n"
    "1. Ask: Experience Level + Risk Tolerance (initial question)\n"
    "2. DO NOT call `set_user_preferences` yet!\n"
    "3. Follow up with additional questions about:\n"
    "   - Maximum position size comfort (% of portfolio per trade, e.g., 15%, 20%, 25%)\n"
    "   - Preferred cash reserve level (% to keep as cash, e.g., 5%, 10%, 15%)\n"
    "   - Maximum drawdown tolerance (% acceptable loss, e.g., 10%, 15%, 20%)\n"
    "   - Trading style preference (conservative, balanced, aggressive)\n"
    "   - Profit scraping percentage (% of profits to set aside, e.g., 20% - or 0% to disable)\n"
    "4. ONLY AFTER collecting all preferences, call `set_user_preferences` with complete profile\n\n"
    "PREFERENCE PARSING FORMAT:\n"
    "When user provides comma-separated preferences like '33%, 10%, 10%, aggressive, 20%':\n"
    "Parse as: position_size_comfort, cash_reserve_level, drawdown_tolerance, trading_style, profit_scraping\n"
    "Always store percentages AS STRINGS with % symbol, e.g.:\n"
    "{\n"
    "  'experience_level': 'advanced',\n"
    "  'risk_tolerance': 'high',\n"
    "  'position_size_comfort': '33%',\n"
    "  'cash_reserve_level': '10%',\n"
    "  'drawdown_tolerance': '10%',\n"
    "  'trading_style': 'aggressive',\n"
    "  'profit_scraping_percentage': '20%'\n"
    "}\n\n"
    "WORKFLOW EXAMPLE for Advanced/Intermediate:\n"
    "User: 'advanced, high risk'\n"
    "You: Ask follow-up questions about position sizing, cash reserves, drawdown tolerance, trading style, profit scraping\n"
    "User: '33%, 10%, 10%, aggressive, 20%'\n"
    "You: Call `set_user_preferences` with preferences object using percentage strings\n\n"
    
    "PERFORMANCE EVALUATION CAPABILITIES:\n"
    "When users ask about execution agent performance, trading results, or system optimization:\n"
    "• Use `trigger_performance_evaluation` to run immediate performance analysis\n"
    "• Use `get_judge_evaluations` to show recent evaluation reports and trends\n"
    "• Use `get_performance_metrics` to display trading statistics and returns\n"
    "• Use `get_risk_metrics` to show current risk exposure and position data\n"
    "• Use `get_transaction_history` to review recent trading activity\n"
    "• Use `get_prompt_history` to show system prompt evolution and versions\n"
    "• Explain evaluation results in clear, business-friendly language\n"
    "• Provide recommendations based on performance analysis\n\n"
    
    "When querying portfolio status, market data, or performance metrics, use the appropriate tools and "
    "provide clear explanations of results and their implications for the user's portfolio. For performance-related "
    "requests, proactively offer to trigger evaluations or show historical analysis to give users comprehensive insights."
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
