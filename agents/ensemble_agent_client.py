import os
import json
import asyncio
from typing import Any
import openai
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent
import time

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a strategy ensemble agent. You periodically evaluate the portfolio "
    "and recent market data to decide whether to trade. You have tools for "
    "fetching portfolio status, retrieving historical ticks, performing risk "
    "checks and placing mock orders. Always call these tools yourself. "
    "Before approving or rejecting any intent, call `get_portfolio_status` to "
    "review cash balances, open positions and entry prices. Use "
    "`get_historical_ticks` to inspect recent prices. When you want to trade, "
    "construct an intent with fields `symbol`, `side`, `qty`, `price` and `ts` "
    "and pass it to `pre_trade_risk_check`. If approved, decide whether to "
    "execute it via `place_mock_order` without waiting for human confirmation, "
    "then briefly explain your decision and the outcome. "
    "Use `get_selected_symbols` to know which trading pairs the broker has "
    "chosen before evaluating the market."
)


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
                    parsed.append(
                        item.model_dump() if hasattr(item, "model_dump") else item
                    )
            return parsed if len(parsed) > 1 else parsed[0]
        return []
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result



async def _evaluate_symbol(
    symbol: str,
    session: ClientSession,
    openai_tools: list[dict[str, Any]],
    portfolio: dict[str, Any],
    ticks: list[dict[str, float]],
) -> None:
    """Evaluate one trading pair using the ensemble agent LLM."""
    conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
    wake_ts = int(time.time())
    last_price = ticks[-1]["price"] if ticks else 0.0
    prompt = (
        f"Periodic check for {symbol} @ {wake_ts}. Portfolio: {json.dumps(portfolio)}. "
        f"Latest price: {last_price}. Recent ticks: {json.dumps(ticks[-10:])}. "
        "Decide whether to BUY, SELL, or hold one unit using the available tools."
    )
    conversation.append({"role": "user", "content": prompt})

    while True:
        response = openai_client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "o4-mini"),
            messages=conversation,
            tools=openai_tools,
            tool_choice="auto",
        )
        msg = response.choices[0].message

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
                func_args = json.loads(tool_call.function.arguments or "{}")
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
        conversation.append({"role": "assistant", "content": assistant_reply})
        print(f"{PINK}[EnsembleAgent] Decision for {symbol}: {assistant_reply}{RESET}")
        break


async def run_ensemble_agent(
    server_url: str = "http://localhost:8080",
    interval_sec: int = 30,
) -> None:
    """Periodically evaluate market data and make trading decisions."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp"
    async with streamablehttp_client(mcp_url) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            tools = tools_resp.tools
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
                if tool.name
                not in {"get_selected_symbols", "get_portfolio_status", "get_historical_ticks"}
            ]
            print(
                "[EnsembleAgent] Connected to MCP server with tools:",
                [t.name for t in tools],
            )

            while True:
                sel_res = await session.call_tool("get_selected_symbols", {})
                symbols = _tool_result_data(sel_res) or []
                if not isinstance(symbols, list) or not symbols:
                    print("[EnsembleAgent] Waiting for broker to select pairs")
                    await asyncio.sleep(interval_sec)
                    continue

                port_res = await session.call_tool("get_portfolio_status", {})
                portfolio = _tool_result_data(port_res)

                async def fetch_ticks(sym: str) -> tuple[str, list[dict[str, float]]]:
                    res = await session.call_tool(
                        "get_historical_ticks", {"symbol": sym, "days": 1}
                    )
                    return sym, _tool_result_data(res)

                tick_map = {
                    sym: ticks
                    for sym, ticks in await asyncio.gather(
                        *(fetch_ticks(sym) for sym in symbols)
                    )
                }

                eval_tasks = [
                    asyncio.create_task(
                        _evaluate_symbol(sym, session, openai_tools, portfolio, tick_map.get(sym, []))
                    )
                    for sym in symbols
                ]
                await asyncio.gather(*eval_tasks)

                await asyncio.sleep(interval_sec)


if __name__ == "__main__":
    asyncio.run(
        run_ensemble_agent(os.environ.get("MCP_SERVER", "http://localhost:8080"))
    )
