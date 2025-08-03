import os
import json
import asyncio
from typing import Any, AsyncIterator, Set
import openai
import logging
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from agents.utils import stream_chat_completion
from mcp.types import CallToolResult, TextContent
from agents.context_manager import create_context_manager
from datetime import timedelta
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
    ScheduleIntervalSpec,
    RPCError,
    RPCStatusCode,
)
from tools.ensemble_nudge import EnsembleNudgeWorkflow
from agents.workflows import BrokerAgentWorkflow, ExecutionAgentWorkflow

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

# Tools this agent is allowed to call
ALLOWED_TOOLS = {
    "place_mock_order",
    "get_historical_ticks",
    "get_portfolio_status",
}

# Context management is now handled by the ContextManager class

NUDGE_SCHEDULE_ID = "ensemble-nudge"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are an autonomous portfolio management agent with moderate risk tolerance. "
    "You operate on scheduled nudges and make data-driven trading decisions for cryptocurrency pairs.\n\n"
    
    "OPERATIONAL WORKFLOW:\n"
    "Each nudge triggers this sequence:\n"
    "1. Call `get_historical_ticks` once with all symbols and latest processed timestamp\n"
    "2. Call `get_portfolio_status` once to get current positions and cash\n"
    "3. Analyze each symbol for trading opportunities\n"
    "4. Execute safety checks before placing any orders\n"
    "5. Submit approved orders and generate summary report\n\n"
    
    "DECISION FRAMEWORK:\n"
    "For each symbol, analyze:\n"
    "• Price momentum and trend direction from recent ticks\n"
    "• Volume patterns and market liquidity\n"
    "• Current position size and portfolio balance\n"
    "• Risk-reward ratio for potential trades\n"
    "• Market correlation and portfolio diversification\n\n"
    
    "Make one of three decisions: BUY, SELL, or HOLD\n"
    "Always provide clear rationale for each decision.\n\n"
    
    "RISK MANAGEMENT:\n"
    "Before executing any trade:\n"
    "• BUY orders: Ensure available cash ≥ (quantity × price × 1.01) for slippage\n"
    "• SELL orders: Ensure current position ≥ desired sell quantity\n"
    "• Limit individual position sizes to reasonable portfolio percentages\n"
    "• If safety checks fail, default to HOLD decision\n\n"
    
    "ORDER EXECUTION:\n"
    "For BUY or SELL decisions, use `place_mock_order` with this exact structure:\n"
    '{\n'
    '  "intent": {\n'
    '    "symbol": <string>,\n'
    '    "side": "BUY" | "SELL",\n'
    '    "qty": <number>,\n'
    '    "price": <number>,\n'
    '    "type": "market" | "limit"\n'
    '  }\n'
    '}\n\n'
    
    "Never submit orders for HOLD decisions.\n\n"
    
    "REPORTING:\n"
    "Provide a structured summary containing:\n"
    "• Analysis and decision for each symbol with rationale\n"
    "• List of orders submitted (if any)\n"
    "• Portfolio impact and risk assessment\n"
    "• Key market observations\n\n"
    
    "Remember the latest timestamp from processed ticks for the next nudge cycle."
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


async def _watch_symbols(client: Client, symbols: Set[str]) -> None:
    """Poll broker workflow for selected symbols."""
    wf_id = os.environ.get("BROKER_WF_ID", "broker-agent")
    while True:
        try:
            handle = client.get_workflow_handle(wf_id)
            syms: list[str] = await handle.query("get_symbols")
            new_set = set(syms)
            if new_set != symbols:
                symbols.clear()
                symbols.update(new_set)
                print(f"[ExecutionAgent] Active symbols updated: {sorted(symbols)}")
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                try:
                    await client.start_workflow(
                        BrokerAgentWorkflow.run,
                        id=wf_id,
                        task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
                    )
                    print("[ExecutionAgent] Broker workflow started")
                except Exception as exc:
                    print(f"[ExecutionAgent] Failed to start broker workflow: {exc}")
            else:
                print(f"[ExecutionAgent] Failed to query broker workflow: {err}")
        except Exception as exc:
            print(f"[ExecutionAgent] Error watching symbols: {exc}")
        await asyncio.sleep(1)


async def _stream_nudges(client: Client) -> AsyncIterator[int]:
    """Yield timestamps from execution-agent workflow nudges."""
    wf_id = os.environ.get("EXECUTION_WF_ID", "execution-agent")
    cursor = 0
    while True:
        try:
            handle = client.get_workflow_handle(wf_id)
            nudges: list[int] = await handle.query("get_nudges")
            for ts in nudges:
                if ts > cursor:
                    cursor = ts
                    yield ts
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                try:
                    await client.start_workflow(
                        ExecutionAgentWorkflow.run,
                        id=wf_id,
                        task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
                    )
                    print("[ExecutionAgent] Execution workflow started")
                except Exception as exc:
                    print(f"[ExecutionAgent] Failed to start execution workflow: {exc}")
            else:
                print(f"[ExecutionAgent] Failed to query execution workflow: {err}")
        except Exception as exc:
            print(f"[ExecutionAgent] Error streaming nudges: {exc}")
        await asyncio.sleep(1)


async def _ensure_schedule(client: Client) -> None:
    """Create the nudge schedule if it doesn't already exist."""
    handle = client.get_schedule_handle(NUDGE_SCHEDULE_ID)
    try:
        await handle.describe()
        return
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            workflow=EnsembleNudgeWorkflow.run,
            args=[],
            id="ensemble-nudge-wf",
            task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=1))]),
    )
    await client.create_schedule(NUDGE_SCHEDULE_ID, schedule)
    await client.get_schedule_handle(NUDGE_SCHEDULE_ID).trigger()


async def run_execution_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the execution agent and act on scheduled nudges."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp/"

    # Initialize context manager
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    context_manager = create_context_manager(model=model, openai_client=openai_client)

    temporal = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    symbols: Set[str] = set()
    _symbol_task = asyncio.create_task(_watch_symbols(temporal, symbols))
    await _ensure_schedule(temporal)

    async with streamablehttp_client(mcp_url) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            all_tools = tools_resp.tools
            tools = [t for t in all_tools if t.name in ALLOWED_TOOLS]
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print(
                "[ExecutionAgent] Connected to MCP server with tools:",
                [t.name for t in tools],
            )

            async for ts in _stream_nudges(temporal):
                if not symbols:
                    continue
                print(f"[ExecutionAgent] Nudge @ {ts} for {sorted(symbols)}")
                conversation.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"nudge": ts, "symbols": sorted(symbols)}
                        ),
                    }
                )
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
                    try:
                        msg = stream_chat_completion(
                            openai_client,
                            model=os.environ.get("OPENAI_MODEL", "o4-mini"),
                            messages=conversation,
                            tools=openai_tools,
                            tool_choice="auto",
                            prefix="[ExecutionAgent] Decision: ",
                            color=PINK,
                            reset=RESET,
                        )
                    except openai.OpenAIError as exc:
                        print(f"[ExecutionAgent] LLM request failed: {exc}")
                        # Keep system prompt and last user message on error
                        if len(conversation) >= 2:
                            conversation = [conversation[0], conversation[-1]]
                        break

                    if msg.get("tool_calls"):
                        conversation.append(
                            {
                                "role": msg.get("role", "assistant"),
                                "content": msg.get("content"),
                                "tool_calls": msg["tool_calls"],
                            }
                        )
                        for tool_call in msg["tool_calls"]:
                            func_name = tool_call["function"]["name"]
                            func_args = json.loads(
                                tool_call["function"].get("arguments") or "{}"
                            )
                            if func_name not in ALLOWED_TOOLS:
                                print(f"[ExecutionAgent] Tool not allowed: {func_name}")
                                continue
                            print(
                                f"{ORANGE}[ExecutionAgent] Tool requested: {func_name} {func_args}{RESET}"
                            )
                            result = await session.call_tool(func_name, func_args)
                            conversation.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "name": func_name,
                                    "content": json.dumps(_tool_result_data(result)),
                                }
                            )
                        continue

                    if msg.get("function_call"):
                        conversation.append(
                            {
                                "role": msg.get("role", "assistant"),
                                "content": msg.get("content"),
                                "function_call": msg["function_call"],
                            }
                        )
                        func_name = msg["function_call"].get("name")
                        func_args = json.loads(
                            msg["function_call"].get("arguments") or "{}"
                        )
                        if func_name not in ALLOWED_TOOLS:
                            print(f"[ExecutionAgent] Tool not allowed: {func_name}")
                            continue
                        print(
                            f"{ORANGE}[ExecutionAgent] Tool requested: {func_name} {func_args}{RESET}"
                        )
                        result = await session.call_tool(func_name, func_args)
                        conversation.append(
                            {
                                "role": "function",
                                "name": func_name,
                                "content": json.dumps(_tool_result_data(result)),
                            }
                        )
                        continue

                    assistant_reply = msg.get("content", "")
                    conversation.append(
                        {"role": "assistant", "content": assistant_reply}
                    )
                    break

                # Manage conversation context intelligently
                conversation = await context_manager.manage_context(conversation)


if __name__ == "__main__":
    asyncio.run(
        run_execution_agent(os.environ.get("MCP_SERVER", "http://localhost:8080"))
    )
