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
import re

EXCHANGE = os.environ.get("EXCHANGE", "coinbaseexchange")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


_NAME_TO_PAIR = {
    "BTC": "BTC/USD",
    "BITCOIN": "BTC/USD",
    "XBT": "BTC/USD",
    "ETH": "ETH/USD",
    "ETHEREUM": "ETH/USD",
    "DOGE": "DOGE/USD",
    "DOGECOIN": "DOGE/USD",
    "LTC": "LTC/USD",
    "LITECOIN": "LTC/USD",
    "ADA": "ADA/USD",
    "CARDANO": "ADA/USD",
    "SOL": "SOL/USD",
    "SOLANA": "SOL/USD",
}


def _parse_symbols(text: str) -> list[str]:
    """Return list of crypto symbols mentioned in ``text``."""
    pairs = re.findall(r"[A-Z0-9]+/[A-Z0-9]+", text.upper())
    tokens = re.split(r"[^A-Z0-9]+", text.upper())
    for token in tokens:
        pair = _NAME_TO_PAIR.get(token)
        if pair and pair not in pairs:
            pairs.append(pair)
    return pairs


async def _prompt_pairs(conversation: list[dict[str, str]]) -> list[str]:
    """Prompt the user for trading pairs, allowing small talk."""
    logger.info("Prompting for trading pairs")
    prompt = (
        "Which crypto pairs would you like to trade? "
        "You can use natural language such as 'Ethereum and Bitcoin' "
        "or specify symbols like 'BTC/USD, ETH/USD': "
    )

    while True:
        text = await asyncio.to_thread(input, prompt)
        if text.strip().lower() in {"quit", "exit"}:
            return []

        pairs = _parse_symbols(text)
        if pairs:
            logger.info("User selected pairs: %s", pairs)
            return pairs

        conversation.append({"role": "user", "content": text})

        if _openai_client is None:
            logger.info("Please specify trading pairs like BTC/USD")
            continue

        try:
            response = _openai_client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                messages=conversation,
            )
            msg = response.choices[0].message
            msg_dict = msg if isinstance(msg, dict) else msg.model_dump()
            assistant_msg = msg_dict.get("content", "")
            conversation.append({"role": "assistant", "content": assistant_msg})
            logger.info("Response: %s", assistant_msg)
            pairs = _parse_symbols(assistant_msg)
            if pairs:
                logger.info("User selected pairs: %s", pairs)
                return pairs
        except Exception as exc:
            logger.error("LLM request failed: %s", exc)



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


async def _start_stream(session: ClientSession, symbols: list[str]) -> None:
    if not symbols:
        return
    payload = {"exchange": EXCHANGE, "symbols": symbols}
    try:
        logger.info("Starting stream for %s", symbols)
        result = await session.call_tool("subscribe_cex_stream", payload)
        data = _tool_result_data(result)
        if isinstance(data, dict):
            wf_id = data.get("workflow_id")
            run_id = data.get("run_id")
        else:
            wf_id = None
            run_id = None
        if wf_id and run_id:
            logger.info(
                "Stream started for %s via workflow %s run %s",
                symbols,
                wf_id,
                run_id,
            )
        else:
            logger.info("Stream started for %s", symbols)
    except Exception as exc:
        logger.error("Failed to start stream: %s", exc)

SYSTEM_PROMPT = (
    "You are a trading broker agent. You manage execution of trades and the account state. "
    "You have tools to place orders, check portfolio status, and handle transactions. "
    "When asked to execute a trade or query account status, use the appropriate tool and report the result."
)

async def get_next_broker_command() -> str | None:
    return await asyncio.to_thread(input, "> ")

async def run_broker_agent(server_url: str = "http://localhost:8080"):
    url = server_url.rstrip("/") + "/mcp"
    logger.info("Connecting to MCP server at %s", url)
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            logger.info("Connected with tools: %s", [t.name for t in tools])

            logger.info("Welcome to %s!", EXCHANGE)
            pairs = await _prompt_pairs(conversation)
            await _start_stream(session, pairs)
            logger.info("Type trade commands like 'buy 1 BTC/USD' or 'status'. 'quit' exits.")

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

                if "tool_calls" in msg_dict:
                    conversation.append(msg_dict)
                    for call in msg_dict["tool_calls"]:
                        func_name = call["function"]["name"]
                        func_args = json.loads(call["function"].get("arguments") or "{}")
                        logger.info("Invoking tool %s with %s", func_name, func_args)
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
                        logger.info("Response: %s", assistant_msg)
                    except Exception as exc:
                        logger.error("LLM request failed: %s", exc)
                        continue
                elif "function_call" in msg_dict:
                    conversation.append(msg_dict)
                    func_name = msg_dict["function_call"]["name"]
                    func_args = json.loads(msg_dict["function_call"].get("arguments") or "{}")
                    logger.info("Invoking tool %s with %s", func_name, func_args)
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
                        logger.info("Response: %s", assistant_msg)
                    except Exception as exc:
                        logger.error("LLM request failed: %s", exc)
                        continue
                else:
                    assistant_msg = msg_dict.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    logger.info("Response: %s", assistant_msg)

                if len(conversation) > 20:
                    conversation = [conversation[0]] + conversation[-19:]

if __name__ == "__main__":
    asyncio.run(run_broker_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
