import os
import json
import asyncio
import logging
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


def _parse_symbols(text: str) -> list[str]:
    """Return list of crypto symbols in ``text``."""
    return re.findall(r"[A-Z0-9]+/[A-Z0-9]+", text.upper())


async def _prompt_pairs() -> list[str]:
    """Prompt the user for trading pairs."""
    logger.info("Prompting for trading pairs")
    text = await asyncio.to_thread(input, "Pairs to trade (e.g. BTC/USD,ETH/USD): ")
    pairs = _parse_symbols(text)
    logger.info("User selected pairs: %s", pairs)
    return pairs


async def _start_stream(session: ClientSession, symbols: list[str]) -> None:
    if not symbols:
        return
    payload = {"exchange": EXCHANGE, "symbols": symbols}
    try:
        logger.info("Starting stream for %s", symbols)
        await session.call_tool("subscribe_cex_stream", payload)
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

            pairs = await _prompt_pairs()
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
                        logger.info("Status: %s", json.dumps(result))
                    except Exception as exc:
                        logger.error("Failed to fetch status: %s", exc)
                    continue

                if _openai_client is None:
                    logger.warning("LLM unavailable; echoing command.")
                    continue

                logger.info("User command: %s", user_request)
                conversation.append({"role": "user", "content": user_request})
                functions = [
                    {"name": t.name, "description": t.description, "parameters": t.inputSchema}
                    for t in tools
                ]
                try:
                    response = _openai_client.chat.completions.create(
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                        tools=functions,
                        tool_choice="auto",
                    )
                    msg = response.choices[0].message
                except Exception as exc:
                    logger.error("LLM request failed: %s", exc)
                    continue

                if msg.get("function_call"):
                    func_name = msg["function_call"]["name"]
                    func_args = json.loads(msg["function_call"].get("arguments") or "{}")
                    logger.info("Invoking tool %s with %s", func_name, func_args)
                    try:
                        result = await session.call_tool(func_name, func_args)
                    except Exception as exc:
                        logger.error("Tool call failed: %s", exc)
                        continue
                    conversation.append({"role": "function", "name": func_name, "content": json.dumps(result)})
                    try:
                        followup = _openai_client.chat.completions.create(
                            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                            messages=conversation,
                            tools=functions,
                        )
                        assistant_msg = followup.choices[0].message.content or ""
                    except Exception as exc:
                        logger.error("LLM request failed: %s", exc)
                        continue
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    logger.info("Response: %s", assistant_msg)
                else:
                    assistant_msg = msg.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    logger.info("Response: %s", assistant_msg)
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

if __name__ == "__main__":
    asyncio.run(run_broker_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
