import os
import json
import asyncio
try:
    import openai
except Exception:  # pragma: no cover - optional dependency
    openai = None
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import re

if openai is not None:
    openai.api_key = os.getenv("OPENAI_API_KEY")

EXCHANGE = os.environ.get("EXCHANGE", "coinbaseexchange")


def _parse_symbols(text: str) -> list[str]:
    """Return list of crypto symbols in ``text``."""
    return re.findall(r"[A-Z0-9]+/[A-Z0-9]+", text.upper())


async def _prompt_pairs() -> list[str]:
    """Prompt the user for trading pairs."""
    text = await asyncio.to_thread(input, "Pairs to trade (e.g. BTC/USD,ETH/USD): ")
    return _parse_symbols(text)


async def _start_stream(session: ClientSession, symbols: list[str]) -> None:
    if not symbols:
        return
    payload = {"exchange": EXCHANGE, "symbols": symbols}
    try:
        await session.call_tool("subscribe_cex_stream", payload)
        print(f"Started stream for: {', '.join(symbols)}")
    except Exception as exc:
        print(f"Failed to start stream: {exc}")

SYSTEM_PROMPT = (
    "You are a trading broker agent. You manage execution of trades and the account state. "
    "You have tools to place orders, check portfolio status, and handle transactions. "
    "When asked to execute a trade or query account status, use the appropriate tool and report the result."
)

async def get_next_broker_command() -> str | None:
    return await asyncio.to_thread(input, "> ")

async def run_broker_agent(server_url: str = "http://localhost:8080"):
    url = server_url.rstrip("/") + "/mcp"
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("[BrokerAgent] Connected with tools:", [t.name for t in tools])

            pairs = await _prompt_pairs()
            await _start_stream(session, pairs)
            print("Type trade commands like 'buy 1 BTC/USD' or 'status'. 'quit' exits.")

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
                        print(json.dumps(result, indent=2))
                    except Exception as exc:
                        print(f"Failed to fetch status: {exc}")
                    continue

                if openai is None or not openai.api_key:
                    print("LLM unavailable; echoing command.")
                    continue

                conversation.append({"role": "user", "content": user_request})
                functions = [
                    {"name": t.name, "description": t.description, "parameters": t.inputSchema}
                    for t in tools
                ]
                try:
                    response = openai.ChatCompletion.create(
                        model=os.environ.get("OPENAI_MODEL", "gpt-4-0613"),
                        messages=conversation,
                        functions=functions,
                        function_call="auto",
                    )
                    msg = response['choices'][0]['message']
                except Exception as exc:
                    print(f"LLM request failed: {exc}")
                    continue

                if msg.get("function_call"):
                    func_name = msg["function_call"]["name"]
                    func_args = json.loads(msg["function_call"].get("arguments") or "{}")
                    print(f"[BrokerAgent] Invoking tool: {func_name}{func_args}")
                    try:
                        result = await session.call_tool(func_name, func_args)
                    except Exception as exc:
                        print(f"Tool call failed: {exc}")
                        continue
                    conversation.append({"role": "function", "name": func_name, "content": json.dumps(result)})
                    try:
                        followup = openai.ChatCompletion.create(
                            model=os.environ.get("OPENAI_MODEL", "gpt-4-0613"),
                            messages=conversation,
                            functions=functions,
                        )
                        assistant_msg = followup['choices'][0]['message']['content']
                    except Exception as exc:
                        print(f"LLM request failed: {exc}")
                        continue
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    print(f"[BrokerAgent] Response: {assistant_msg}")
                else:
                    assistant_msg = msg.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    print(f"[BrokerAgent] Response: {assistant_msg}")
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

if __name__ == "__main__":
    asyncio.run(run_broker_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
