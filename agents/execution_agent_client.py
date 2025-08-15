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
from agents.workflows.broker_agent_workflow import BrokerAgentWorkflow
from agents.workflows.execution_agent_workflow import ExecutionAgentWorkflow
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
    "You are an autonomous portfolio management agent that analyzes market data and executes trading decisions "
    "based on user preferences and risk profile. You operate independently without human confirmation.\n\n"
    
    "AUTONOMOUS OPERATION:\n"
    "• Make all trading decisions independently - no human approval required\n"
    "• Execute orders immediately when your analysis indicates action\n"
    "• Never ask for confirmation or present multiple choice options\n"
    "• Report what you decided and executed, not what you recommend\n\n"
    
    "DATA ANALYSIS:\n"
    "• Combine new tick data with your conversation history for complete market picture\n"
    "• Analyze price momentum, trends, support/resistance, and volume patterns\n"
    "• Consider current portfolio, performance metrics, and risk exposure\n"
    "• Apply user risk tolerance and trading style preferences\n\n"
    
    "RISK MANAGEMENT:\n"
    "• Size positions according to position_size_comfort limit\n"
    "• Verify sufficient cash for buys and holdings for sells\n"
    "• Apply profit-taking and stop-loss based on user preferences and market conditions\n"
    "• Account for slippage in order sizing\n\n"
    
    "ORDER FORMAT:\n"
    "Single order:\n"
    '{"intent": {"symbol": "BTC", "side": "BUY", "qty": 100, "price": 50000, "type": "market"}}\n\n'
    
    "Batch orders (preferred for multiple trades):\n"
    '{"intent": {"orders": [{"symbol": "BTC", "side": "BUY", "qty": 100, "price": 50000, "type": "market"}, '
    '{"symbol": "ETH", "side": "SELL", "qty": 50, "price": 3000, "type": "limit"}]}}\n\n'
    
    "Execute trades decisively using `place_mock_order`. Report completed actions and reasoning."
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
                
                # The judge agent will handle system prompt updates directly
                
        except Exception as exc:
            # Silently continue if execution agent workflow not found or other issues
            pass
        
        await asyncio.sleep(2)  # Check every 2 seconds


async def _watch_system_prompt_updates(client: Client, conversation: list) -> None:
    """Watch for system prompt updates from the judge agent."""
    wf_id = "execution-agent"
    last_prompt = ""
    
    while True:
        try:
            handle = client.get_workflow_handle(wf_id)
            current_prompt = await handle.query("get_system_prompt")
            
            # Check if prompt has changed
            if current_prompt and current_prompt != last_prompt:
                if conversation and conversation[0]["role"] == "system":
                    conversation[0]["content"] = current_prompt
                    print(f"[ExecutionAgent] 🔄 System prompt updated by judge (length: {len(current_prompt)} chars)")
                    last_prompt = current_prompt
                
        except Exception as exc:
            # Silently continue if execution agent workflow not found or other issues
            pass
        
        await asyncio.sleep(5)  # Check every 5 seconds


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
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=25))]),
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
    
    # Initialize context manager
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    context_manager = create_context_manager(model=model, openai_client=openai_client)
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
            
            # Get current system prompt from workflow, fallback to default
            try:
                handle = temporal.get_workflow_handle("execution-agent")
                current_prompt = await handle.query("get_system_prompt")
                if not current_prompt:
                    current_prompt = SYSTEM_PROMPT
                    # Initialize workflow with default prompt
                    await handle.signal("update_system_prompt", SYSTEM_PROMPT)
                print(f"[ExecutionAgent] Using system prompt from workflow (length: {len(current_prompt)} chars)")
            except Exception as exc:
                current_prompt = SYSTEM_PROMPT
                print(f"[ExecutionAgent] Using fallback system prompt: {exc}")
            
            conversation = [{"role": "system", "content": current_prompt}]
            print(
                "[ExecutionAgent] Connected to MCP server with tools:",
                [t.name for t in tools],
            )

            # Start watching for user preference updates
            _preferences_task = asyncio.create_task(_watch_user_preferences(temporal, current_preferences, conversation))
            
            # Start watching for system prompt updates from judge
            _prompt_task = asyncio.create_task(_watch_system_prompt_updates(temporal, conversation))

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
                
                # Determine since_ts for incremental data fetching
                if latest_processed_ts is not None:
                    since_ts = latest_processed_ts
                    print(f"[ExecutionAgent] Fetching NEW ticks since timestamp: {since_ts}")
                else:
                    since_ts = 0  # Get all historical data on first run
                    print(f"[ExecutionAgent] First run - fetching all available historical data (since_ts=0)")
                
                # Start all data collection tasks in parallel
                tasks = [
                    session.call_tool("get_historical_ticks", {
                        "symbols": sorted(symbols),
                        "since_ts": since_ts  # Incremental fetching to avoid context bloat
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
                    new_tick_count = 0
                    
                    for symbol, symbol_data in historical_ticks_data.items():
                        if isinstance(symbol_data, list) and symbol_data:
                            new_tick_count += len(symbol_data)
                            # Get the latest timestamp from this symbol's ticks
                            for tick in symbol_data:
                                if isinstance(tick, dict):
                                    # Try both 'ts' and 'timestamp' fields
                                    tick_ts = tick.get('ts', 0) or tick.get('timestamp', 0)
                                    if tick_ts > 0:
                                        max_timestamp = max(max_timestamp, tick_ts)
                    
                    if max_timestamp > 0:
                        latest_processed_ts = max_timestamp
                        print(f"[ExecutionAgent] Received {new_tick_count} new ticks (latest timestamp: {latest_processed_ts})")
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
                            model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
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
                        # Process each tool call (batch orders are handled by MCP server)
                        for tool_call in msg["tool_calls"]:
                            func_name = tool_call["function"]["name"]
                            func_args = json.loads(
                                tool_call["function"].get("arguments") or "{}"
                            )
                            # Only allow order execution in LLM phase (data collection handled separately)
                            if func_name != "place_mock_order":
                                print(f"[ExecutionAgent] Tool not allowed in analysis phase: {func_name}")
                                continue
                            
                            # Check if this is a batch order
                            intent = func_args.get("intent", {})
                            is_batch = "orders" in intent
                            order_count = len(intent["orders"]) if is_batch else 1
                            
                            print(
                                f"{ORANGE}[ExecutionAgent] Tool requested: {func_name} "
                                f"({'batch: ' + str(order_count) + ' orders' if is_batch else 'single order'}) "
                                f"{RESET}"
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
