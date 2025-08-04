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
from agents.prompt_manager import create_prompt_manager
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
from tools.agent_logger import AgentLogger

ORANGE = "\033[33m"
PINK = "\033[95m"
RESET = "\033[0m"

# Tools this agent is allowed to call
ALLOWED_TOOLS = {
    "place_mock_order",
    "get_historical_ticks",
    "get_portfolio_status",
    "get_user_preferences",
    "get_transaction_history",
    "get_performance_metrics",
    "get_risk_metrics",
}

# Context management is now handled by the ContextManager class

NUDGE_SCHEDULE_ID = "ensemble-nudge"



logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are an autonomous portfolio management agent with adaptive risk tolerance. "
    "You analyze comprehensive market data and make data-driven trading decisions for cryptocurrency pairs "
    "based on user preferences and risk profile.\n\n"
    
    "DATA-DRIVEN ANALYSIS WORKFLOW:\n"
    "You will receive complete market data including:\n"
    "• Historical price ticks with volume and timestamps\n"
    "• Current portfolio positions, cash, and P&L\n"
    "• User risk preferences and trading style\n"
    "• Recent performance metrics and trading success rates\n"
    "• Current risk exposure and portfolio concentration\n\n"
    
    "YOUR ROLE:\n"
    "Analyze the provided data and make trading decisions. You do NOT need to call data collection tools - "
    "all necessary data is provided in the input. Focus on analysis and decision-making.\n\n"
    
    "DECISION FRAMEWORK:\n"
    "For each symbol, analyze:\n"
    "• Price momentum and trend direction from recent ticks\n"
    "• Current position size and portfolio balance\n"
    "• User risk tolerance and trading preferences\n"
    "• Recent performance metrics and trading success rates\n"
    "• Current portfolio risk exposure and concentration\n"
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
    
    "NOTE: Data continuity is handled automatically by the system. Focus on analysis and decision-making."
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


async def _watch_user_preferences(client: Client, current_preferences: dict, conversation: list) -> None:
    """Poll execution agent workflow for user preference updates."""
    wf_id = "execution-agent"
    while True:
        try:
            handle = client.get_workflow_handle(wf_id)
            prefs = await handle.query("get_user_preferences")
            
            # Check if preferences have changed
            if prefs != current_preferences:
                current_preferences.clear()
                current_preferences.update(prefs)
                print(f"[ExecutionAgent] ✅ User preferences updated: risk_tolerance={prefs.get('risk_tolerance', 'moderate')}, style={prefs.get('trading_style', 'unknown')}")
                
                # Update system prompt with new preferences
                await _update_system_prompt(client, prefs, conversation)
        except Exception as exc:
            # Silently continue if execution agent workflow not found or other issues
            pass
        
        await asyncio.sleep(2)  # Check every 2 seconds


async def _update_system_prompt(client: Client, user_preferences: dict, conversation: list) -> None:
    """Update the system prompt with new user preferences."""
    if not conversation or conversation[0]["role"] != "system":
        return
        
    try:
        prompt_manager = await create_prompt_manager(temporal_client=client)
        
        context = {
            "risk_mode": user_preferences.get("risk_tolerance", "moderate"),
            "performance_trend": ["stable"]  # Default to stable, judge can update this
        }
        updated_prompt = await prompt_manager.get_current_prompt("execution_agent", context)
        
        # Update the conversation
        conversation[0]["content"] = updated_prompt
        print(f"[ExecutionAgent] 🔄 System prompt updated with {user_preferences.get('risk_tolerance', 'moderate')} risk tolerance")
    except Exception as exc:
        print(f"[ExecutionAgent] Failed to update system prompt: {exc}")


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

    temporal = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    
    # Initialize context and prompt managers
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    context_manager = create_context_manager(model=model, openai_client=openai_client)
    prompt_manager = await create_prompt_manager(temporal_client=temporal)
    symbols: Set[str] = set()
    current_preferences: dict = {}
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
            
            # Initialize with default prompt - will be updated after user preferences are retrieved
            current_prompt = SYSTEM_PROMPT
            
            conversation = [{"role": "system", "content": current_prompt}]
            print(
                "[ExecutionAgent] Connected to MCP server with tools:",
                [t.name for t in tools],
            )

            # Start watching for user preference updates
            _preferences_task = asyncio.create_task(_watch_user_preferences(temporal, current_preferences, conversation))

            # Track latest processed timestamp for data continuity
            latest_processed_ts = None
            
            # Initialize agent logger
            agent_logger = AgentLogger("execution_agent", temporal)
            
            async for ts in _stream_nudges(temporal):
                if not symbols:
                    continue
                print(f"[ExecutionAgent] Nudge @ {ts} for {sorted(symbols)}")
                
                # ===============================
                # DETERMINISTIC DATA COLLECTION PHASE (PARALLEL)
                # ===============================
                print(f"[ExecutionAgent] Starting mandatory data collection (parallel)...")
                
                # Determine since_ts for data continuity
                if latest_processed_ts is not None:
                    since_ts = latest_processed_ts
                    print(f"[ExecutionAgent] Using latest processed timestamp: {since_ts}")
                else:
                    since_ts = ts - 60  # Initial fallback: 60 seconds ago
                    print(f"[ExecutionAgent] No previous timestamp, using fallback: {since_ts}")
                
                # Start all data collection tasks in parallel
                tasks = [
                    session.call_tool("get_historical_ticks", {
                        "symbols": sorted(symbols),
                        "since_ts": since_ts
                    }),
                    session.call_tool("get_portfolio_status", {}),
                    session.call_tool("get_user_preferences", {}),
                    session.call_tool("get_performance_metrics", {}),
                    session.call_tool("get_risk_metrics", {})
                ]
                
                # Wait for all tasks to complete
                results = await asyncio.gather(*tasks)
                historical_data, portfolio_data, user_preferences, performance_metrics, risk_metrics = results
                
                print(f"[ExecutionAgent] ✓ Collected all data in parallel")
                
                # Extract and update latest processed timestamp for next cycle
                historical_ticks_data = _tool_result_data(historical_data)
                
                if historical_ticks_data and isinstance(historical_ticks_data, dict):
                    # Find the latest timestamp from all symbols' tick data
                    max_timestamp = 0
                    
                    for symbol, symbol_data in historical_ticks_data.items():
                        if isinstance(symbol_data, list) and symbol_data:
                            # Get the latest timestamp from this symbol's ticks
                            for tick in symbol_data:
                                if isinstance(tick, dict):
                                    # Try both 'ts' and 'timestamp' fields
                                    tick_ts = tick.get('ts', 0) or tick.get('timestamp', 0)
                                    if tick_ts > 0:
                                        max_timestamp = max(max_timestamp, tick_ts)
                    
                    if max_timestamp > 0:
                        latest_processed_ts = max_timestamp
                        print(f"[ExecutionAgent] Updated latest processed timestamp: {latest_processed_ts}")
                    else:
                        print(f"[ExecutionAgent] Warning: No valid timestamps found in tick data")
                
                # Compile all data for LLM analysis
                collected_data = {
                    "nudge_timestamp": ts,
                    "symbols": sorted(symbols),
                    "historical_ticks": _tool_result_data(historical_data),
                    "portfolio_status": _tool_result_data(portfolio_data),
                    "user_preferences": _tool_result_data(user_preferences),
                    "performance_metrics": _tool_result_data(performance_metrics),
                    "risk_metrics": _tool_result_data(risk_metrics)
                }
                
                print(f"[ExecutionAgent] Data collection complete. Starting analysis phase...")
                
                # ===============================
                # LLM ANALYSIS PHASE
                # ===============================
                conversation.append(
                    {
                        "role": "user",
                        "content": json.dumps(collected_data),
                    }
                )
                # Only provide order execution tool to LLM (data collection is handled deterministically)
                order_tool = next((t for t in tools if t.name == "place_mock_order"), None)
                openai_tools = []
                if order_tool:
                    openai_tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": order_tool.name,
                                "description": order_tool.description,
                                "parameters": order_tool.inputSchema,
                            },
                        }
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
                            # Only allow order execution in LLM phase (data collection handled separately)
                            if func_name != "place_mock_order":
                                print(f"[ExecutionAgent] Tool not allowed in analysis phase: {func_name}")
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
                        # Only allow order execution in LLM phase (data collection handled separately)
                        if func_name != "place_mock_order":
                            print(f"[ExecutionAgent] Tool not allowed in analysis phase: {func_name}")
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
                    
                    # Log comprehensive decision with all context
                    try:
                        # Extract decisions from the current cycle only (tool calls made in this response)
                        decisions_made = []
                        if msg.get("tool_calls"):
                            for tool_call in msg["tool_calls"]:
                                if tool_call["function"]["name"] == "place_mock_order":
                                    order_args = json.loads(tool_call["function"]["arguments"])
                                    decisions_made.append({
                                        "action": "place_order",
                                        "details": order_args
                                    })
                        
                        # Log the comprehensive decision
                        await agent_logger.log_decision(
                            nudge_timestamp=ts,
                            symbols=sorted(symbols),
                            market_data={"historical_ticks": _tool_result_data(historical_data)},
                            portfolio_data=_tool_result_data(portfolio_data),
                            user_preferences=_tool_result_data(user_preferences),
                            decisions={
                                "orders_placed": len(decisions_made),
                                "decisions": decisions_made,
                                "hold_decisions": len(symbols) - len(decisions_made)
                            },
                            reasoning=assistant_reply,
                            performance_metrics=_tool_result_data(performance_metrics),
                            risk_metrics=_tool_result_data(risk_metrics),
                            latest_processed_timestamp=latest_processed_ts
                        )
                        
                        # Log each individual action
                        for decision in decisions_made:
                            agent_logger.log_action(
                                action_type="order_placement",
                                details=decision["details"],
                                nudge_timestamp=ts
                            )
                            
                    except Exception as log_error:
                        print(f"[ExecutionAgent] Failed to log decision: {log_error}")
                    
                    break

                # Manage conversation context intelligently
                conversation = await context_manager.manage_context(conversation)


if __name__ == "__main__":
    asyncio.run(
        run_execution_agent(os.environ.get("MCP_SERVER", "http://localhost:8080"))
    )
