"""MCP server exposing Temporal workflows as tools."""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any, Dict, List
from datetime import datetime, timezone
import json

from mcp.server.fastmcp import FastMCP
import logging
from temporalio.client import Client, RPCError, RPCStatusCode, WorkflowExecutionStatus
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import Request

# Import workflow classes
from tools.market_data import SubscribeCEXStream
from tools.strategy_signal import EvaluateStrategyMomentum
from tools.execution import PlaceMockOrder, OrderIntent
from agents.workflows import (
    ExecutionLedgerWorkflow,
    BrokerAgentWorkflow,
    JudgeAgentWorkflow,
)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Initialize FastMCP
app = FastMCP("crypto-trading-server")

# Shared Temporal client
_temporal_client: Client | None = None
_client_lock = asyncio.Lock()

# Simple in-memory signal log for backward compatibility
signal_log: dict[str, list[dict]] = {}


async def get_temporal_client() -> Client:
    """Connect to Temporal server (lazy singleton)."""
    global _temporal_client
    if _temporal_client is None:
        async with _client_lock:
            if _temporal_client is None:
                address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
                namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
                logger.info("Connecting to Temporal at %s (ns=%s)", address, namespace)
                _temporal_client = await Client.connect(address, namespace=namespace)
                logger.info("Temporal client ready")
    return _temporal_client


@app.tool(annotations={"title": "Subscribe CEX Stream", "readOnlyHint": True})
async def subscribe_cex_stream(
    symbols: List[str], interval_sec: int = 1
) -> Dict[str, str]:
    """Start a durable workflow to stream market data from a CEX.

    Parameters
    ----------
    symbols:
        List of asset pairs in ``BASE/QUOTE`` format.
    interval_sec:
        Number of seconds between ticker fetches.

    Returns
    -------
    Dict[str, str]
        ``workflow_id`` and ``run_id`` of the started workflow.
    """
    client = await get_temporal_client()
    workflow_id = f"stream-{secrets.token_hex(4)}"
    logger.info(
        "Starting SubscribeCEXStream: coinbase %s interval=%s",
        symbols,
        interval_sec,
    )
    logger.debug("Launching workflow %s", workflow_id)
    try:
        handle = await client.start_workflow(
            SubscribeCEXStream.run,
            args=[symbols, interval_sec],
            id=workflow_id,
            task_queue="mcp-tools",
        )
    except Exception:
        logger.exception("Failed to start SubscribeCEXStream workflow %s", workflow_id)
        raise
    logger.debug("Workflow handle created: %s", handle)
    run_id = handle.first_execution_run_id or handle.result_run_id
    logger.info("Workflow %s started run %s", workflow_id, run_id)
    return {"workflow_id": workflow_id, "run_id": run_id}


@app.tool(annotations={"title": "Start Market Stream", "readOnlyHint": True})
async def start_market_stream(
    symbols: List[str], interval_sec: int = 1
) -> Dict[str, str]:
    """Convenience wrapper around ``subscribe_cex_stream``.

    Also records the selected symbols for the execution agent.

    Parameters
    ----------
    symbols:
        Trading pairs to stream.
    interval_sec:
        Seconds between ticker fetches.

    Returns
    -------
    Dict[str, str]
        ``workflow_id`` and ``run_id`` of the started workflow.
    """
    result = await subscribe_cex_stream(symbols, interval_sec)
    client = await get_temporal_client()
    wf_id = os.environ.get("BROKER_WF_ID", "broker-agent")
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("set_symbols", symbols)
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                BrokerAgentWorkflow.run,
                id=wf_id,
                task_queue="mcp-tools",
            )
            await handle.signal("set_symbols", symbols)
        else:
            raise
    return result


@app.tool(annotations={"title": "Set User Preferences", "readOnlyHint": False})
async def set_user_preferences(preferences: Dict[str, Any]) -> Dict[str, str]:
    """Set user trading preferences including risk tolerance.

    Parameters
    ----------
    preferences:
        Dictionary of user preferences including risk_tolerance, experience_level, etc.

    Returns
    -------
    Dict[str, str]
        Confirmation of preferences set.
    """
    client = await get_temporal_client()
    wf_id = os.environ.get("BROKER_WF_ID", "broker-agent")
    logger.info("Setting user preferences: %s", preferences)
    
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                BrokerAgentWorkflow.run,
                id=wf_id,
                task_queue="mcp-tools",
            )
        else:
            raise
    
    await handle.signal("set_user_preferences", preferences)
    logger.info("User preferences updated successfully")
    
    return {
        "status": "success",
        "message": f"Updated user preferences: {', '.join(preferences.keys())}",
        "preferences_set": str(list(preferences.keys()))
    }


@app.tool(annotations={"title": "Get User Preferences", "readOnlyHint": True})
async def get_user_preferences() -> Dict[str, Any]:
    """Get current user trading preferences.

    Returns
    -------
    Dict[str, Any]
        Current user preferences including risk tolerance, experience level, etc.
    """
    client = await get_temporal_client()
    wf_id = os.environ.get("BROKER_WF_ID", "broker-agent")
    logger.info("Retrieving user preferences")
    
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                BrokerAgentWorkflow.run,
                id=wf_id,
                task_queue="mcp-tools",
            )
        else:
            raise
    
    preferences = await handle.query("get_user_preferences")
    logger.info("Retrieved user preferences")
    
    return preferences


@app.tool(annotations={"title": "Evaluate Momentum Strategy", "readOnlyHint": True})
async def evaluate_strategy_momentum(
    signal: Dict[str, Any], cooldown_sec: int = 0
) -> Dict[str, Any]:
    """Invoke the momentum strategy evaluation workflow.

    Parameters
    ----------
    signal:
        Raw strategy signal payload.
    cooldown_sec:
        Optional delay after logging the signal.

    Returns
    -------
    Dict[str, Any]
        The logged ``signal`` payload.
    """
    client = await get_temporal_client()
    workflow_id = f"momentum-{secrets.token_hex(4)}"
    logger.info("Evaluating momentum strategy: cooldown=%s", cooldown_sec)
    handle = await client.start_workflow(
        EvaluateStrategyMomentum.run,
        args=[signal, cooldown_sec or None],
        id=workflow_id,
        task_queue="mcp-tools",
    )
    result = await handle.result()
    logger.info("Momentum workflow %s completed", workflow_id)
    return result


@app.tool(
    annotations={
        "title": "Place Mock Order",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
async def place_mock_order(intent: OrderIntent) -> Dict[str, Any]:
    """Simulate executing an order intent.

    Parameters
    ----------
    intent:
        Order details with keys ``symbol``, ``side``, ``qty``, ``price`` and ``type``.

    Returns
    -------
    Dict[str, Any]
        Fill information including ``fill_price`` and ``cost``.
    """
    client = await get_temporal_client()
    workflow_id = f"order-{secrets.token_hex(4)}"
    logger.info("Placing mock order via workflow %s", workflow_id)
    handle = await client.start_workflow(
        PlaceMockOrder.run,
        intent,
        id=workflow_id,
        task_queue="mcp-tools",
    )
    fill = await handle.result()
    logger.info("Order workflow %s completed", workflow_id)
    try:
        ledger_wf_id = os.environ.get("LEDGER_WF_ID", "mock-ledger")
        ledger = client.get_workflow_handle(ledger_wf_id)
        
        # Ensure ledger workflow exists
        try:
            await ledger.describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                ledger = await client.start_workflow(
                    ExecutionLedgerWorkflow.run,
                    id=ledger_wf_id,
                    task_queue="mcp-tools",
                )
                logger.info("Started ledger workflow %s", ledger_wf_id)
            else:
                raise
        
        await ledger.signal("record_fill", fill)
        logger.info("Recorded fill in ledger: %s %s %s", fill["side"], fill["qty"], fill["symbol"])
    except Exception as exc:
        logger.error("Failed to record fill in ledger: %s", exc)
    return fill

@app.tool(annotations={"title": "Get Historical Ticks", "readOnlyHint": True})
async def get_historical_ticks(
    symbols: List[str] | None = None,
    symbol: str | None = None,
    days: int | None = None,
    since_ts: int | None = None,
) -> Dict[str, List[Dict[str, float]]]:
    """Return historical ticks for one or more symbols.

    Parameters
    ----------
    symbols:
        List of asset pairs in ``BASE/QUOTE`` format.
    symbol:
        Single asset pair if ``symbols`` not provided (for backward compatibility).
    days:
        Number of days of history requested. ``None`` (default) returns **all**
        stored ticks.
    since_ts:
        Unix timestamp in seconds. If provided, overrides ``days`` and returns
        ticks at or after this time.
    """

    if symbols is None:
        if symbol is None:
            raise ValueError("symbol or symbols required")
        symbols = [symbol]

    if since_ts is not None:
        cutoff = since_ts
    else:
        cutoff = 0 if days is None else int(datetime.now(timezone.utc).timestamp()) - days * 86400
    client = await get_temporal_client()
    results: Dict[str, List[Dict[str, float]]] = {}
    for sym in symbols:
        wf_id = f"feature-{sym.replace('/', '-')}"
        logger.info("Querying workflow %s for ticks >= %d", wf_id, cutoff)
        handle = client.get_workflow_handle(wf_id)
        try:
            ticks_raw = await handle.query("historical_ticks", cutoff)
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                logger.warning("Feature workflow %s not found", wf_id)
                results[sym] = []
                continue
            raise

        ticks = [
            {"ts": int(t["ts"]), "price": float(t["price"])}
            for t in ticks_raw
        ]
        logger.info("Retrieved %d ticks for %s", len(ticks), sym)
        results[sym] = ticks

    return results


@app.tool(annotations={"title": "Get Portfolio Status", "readOnlyHint": True})
async def get_portfolio_status() -> Dict[str, Any]:
    """Retrieve current portfolio cash and positions from the ledger.

    Returns
    -------
    Dict[str, Any]
        Cash balance, open positions, entry prices and PnL.
    """
    client = await get_temporal_client()
    wf_id = os.environ.get("LEDGER_WF_ID", "mock-ledger")
    logger.info("Fetching portfolio status from %s", wf_id)
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                ExecutionLedgerWorkflow.run,
                id=wf_id,
                task_queue="mcp-tools",
            )
        else:
            raise
    cash = await handle.query("get_cash")
    positions = await handle.query("get_positions")
    entry_prices = await handle.query("get_entry_prices")
    pnl = await handle.query("get_pnl")
    logger.info("Ledger status retrieved")
    return {
        "cash": cash,
        "positions": positions,
        "entry_prices": entry_prices,
        "pnl": pnl,
    }


@app.tool(annotations={"title": "Get Transaction History", "readOnlyHint": True})
async def get_transaction_history(
    since_ts: int = 0, limit: int = 1000
) -> Dict[str, Any]:
    """Get transaction history from the execution ledger.
    
    Parameters
    ----------
    since_ts:
        Unix timestamp in seconds. Only transactions at or after this time.
    limit:
        Maximum number of transactions to return.
        
    Returns
    -------
    Dict[str, Any]
        Transaction history with metadata.
    """
    client = await get_temporal_client()
    wf_id = os.environ.get("LEDGER_WF_ID", "mock-ledger")
    logger.info("Fetching transaction history from %s", wf_id)
    
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                ExecutionLedgerWorkflow.run,
                id=wf_id,
                task_queue="mcp-tools",
            )
        else:
            raise
    
    transactions = await handle.query("get_transaction_history", {"since_ts": since_ts, "limit": limit})
    logger.info("Retrieved %d transactions", len(transactions))
    
    return {
        "transactions": transactions,
        "count": len(transactions),
        "since_timestamp": since_ts,
        "limit": limit
    }


@app.tool(annotations={"title": "Get Performance Metrics", "readOnlyHint": True})
async def get_performance_metrics(window_days: int = 30) -> Dict[str, Any]:
    """Get performance metrics from the execution ledger.
    
    Parameters
    ----------
    window_days:
        Number of days to analyze for performance metrics.
        
    Returns
    -------
    Dict[str, Any]
        Performance metrics including returns, drawdown, trade statistics.
    """
    client = await get_temporal_client()
    wf_id = os.environ.get("LEDGER_WF_ID", "mock-ledger")
    logger.info("Fetching performance metrics from %s for %d days", wf_id, window_days)
    
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                ExecutionLedgerWorkflow.run,
                id=wf_id,
                task_queue="mcp-tools",
            )
        else:
            raise
    
    metrics = await handle.query("get_performance_metrics", window_days)
    logger.info("Retrieved performance metrics")
    
    return metrics


@app.tool(annotations={"title": "Get Risk Metrics", "readOnlyHint": True})
async def get_risk_metrics() -> Dict[str, Any]:
    """Get current risk metrics from the execution ledger.
        
    Returns
    -------
    Dict[str, Any]
        Risk metrics including position concentration, leverage, cash ratio.
    """
    client = await get_temporal_client()
    wf_id = os.environ.get("LEDGER_WF_ID", "mock-ledger")
    logger.info("Fetching risk metrics from %s", wf_id)
    
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                ExecutionLedgerWorkflow.run,
                id=wf_id,
                task_queue="mcp-tools",
            )
        else:
            raise
    
    metrics = await handle.query("get_risk_metrics")
    logger.info("Retrieved risk metrics")
    
    return metrics


@app.tool(annotations={"title": "Trigger Performance Evaluation", "readOnlyHint": False})
async def trigger_performance_evaluation(
    window_days: int = 7, force: bool = False
) -> Dict[str, Any]:
    """Trigger a performance evaluation by the judge agent.
    
    Parameters
    ----------
    window_days:
        Number of days to analyze for the evaluation.
    force:
        Force evaluation even if cooldown period hasn't elapsed.
        
    Returns
    -------
    Dict[str, Any]
        Evaluation trigger result.
    """
    client = await get_temporal_client()
    judge_wf_id = os.environ.get("JUDGE_WF_ID", "judge-agent")
    logger.info("Triggering performance evaluation")
    
    try:
        handle = client.get_workflow_handle(judge_wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            handle = await client.start_workflow(
                JudgeAgentWorkflow.run,
                id=judge_wf_id,
                task_queue="mcp-tools",
            )
            logger.info("Started judge agent workflow")
        else:
            raise
    
    # Check if evaluation should be triggered
    if not force:
        should_evaluate = await handle.query("should_trigger_evaluation", 4)
        if not should_evaluate:
            return {
                "triggered": False,
                "reason": "Evaluation cooldown period has not elapsed",
                "suggestion": "Use force=true to override cooldown"
            }
    
    # Create evaluation trigger signal
    trigger_data = {
        "window_days": window_days,
        "trigger_timestamp": int(datetime.now(timezone.utc).timestamp()),
        "force": force
    }
    
    # Signal the judge agent to trigger an immediate evaluation
    await handle.signal("trigger_immediate_evaluation", {
        "window_days": window_days,
        "forced": force,
        "trigger_timestamp": trigger_data["trigger_timestamp"]
    })
    
    logger.info("Performance evaluation triggered")
    
    return {
        "triggered": True,
        "window_days": window_days,
        "forced": force,
        "message": "Performance evaluation has been requested"
    }


@app.tool(annotations={"title": "Get Judge Evaluations", "readOnlyHint": True})
async def get_judge_evaluations(limit: int = 20, since_ts: int = 0) -> Dict[str, Any]:
    """Get recent performance evaluations from the judge agent.
    
    Parameters
    ----------
    limit:
        Maximum number of evaluations to return.
    since_ts:
        Unix timestamp in seconds. Only evaluations at or after this time.
        
    Returns
    -------
    Dict[str, Any]
        Recent evaluations and performance trends.
    """
    client = await get_temporal_client()
    judge_wf_id = os.environ.get("JUDGE_WF_ID", "judge-agent")
    logger.info("Fetching judge evaluations")
    
    try:
        handle = client.get_workflow_handle(judge_wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            logger.warning("Judge workflow not found")
            return {
                "evaluations": [],
                "count": 0,
                "trend": {"trend": "unknown", "avg_score": 0.0}
            }
        else:
            raise
    
    evaluations = await handle.query("get_evaluations", {"limit": limit, "since_ts": since_ts})
    trend = await handle.query("get_performance_trend", 30)
    
    logger.info("Retrieved %d evaluations", len(evaluations))
    
    return {
        "evaluations": evaluations,
        "count": len(evaluations),
        "trend": trend
    }


@app.tool(annotations={"title": "Get Prompt History", "readOnlyHint": True})
async def get_prompt_history(limit: int = 10) -> Dict[str, Any]:
    """Get prompt version history from the judge agent.
    
    Parameters
    ----------
    limit:
        Maximum number of prompt versions to return.
        
    Returns
    -------
    Dict[str, Any]
        Prompt version history and current active version.
    """
    client = await get_temporal_client()
    judge_wf_id = os.environ.get("JUDGE_WF_ID", "judge-agent")
    logger.info("Fetching prompt history")
    
    try:
        handle = client.get_workflow_handle(judge_wf_id)
        await handle.describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            logger.warning("Judge workflow not found")
            return {
                "versions": [],
                "current_version": {},
                "count": 0
            }
        else:
            raise
    
    versions = await handle.query("get_prompt_versions", limit)
    current_version = await handle.query("get_current_prompt_version")
    
    logger.info("Retrieved %d prompt versions", len(versions))
    
    return {
        "versions": versions,
        "current_version": current_version,
        "count": len(versions)
    }


@app.custom_route("/workflow/{workflow_id}/{run_id}", methods=["GET"])
async def workflow_status(request: Request) -> Response:
    workflow_id = request.path_params["workflow_id"]
    run_id = request.path_params["run_id"]
    logger.info("Fetching status for %s %s", workflow_id, run_id)
    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    desc = await handle.describe()
    status_name = desc.status.name if desc.status else "UNKNOWN"
    result: Any | None = None
    if desc.status and desc.status != WorkflowExecutionStatus.RUNNING:
        try:
            result = await handle.result()
        except Exception as exc:
            result = {"error": str(exc)}
    logger.info("Workflow %s status %s", workflow_id, status_name)
    return JSONResponse({"status": status_name, "result": result})


@app.custom_route("/signal/{name}", methods=["POST"])
async def record_signal(request: Request) -> Response:
    """Record a signal event for services still using HTTP polling."""
    name = request.path_params["name"]
    logger.debug("Recording signal %s", name)
    payload = await request.json()
    ts = payload.get("ts")
    if ts is None:
        ts = int(datetime.now(timezone.utc).timestamp())
        payload["ts"] = ts
    signal_log.setdefault(name, []).append(payload)
    logger.debug("Recorded signal %s", name)
    return Response(status_code=204)


@app.custom_route("/signal/{name}", methods=["GET"])
async def fetch_signals(request: Request) -> Response:
    """Stream signal events newer than the provided ``after`` timestamp."""

    name = request.path_params["name"]
    after = int(request.query_params.get("after", "0"))

    async def event_stream() -> Any:
        cursor = after
        idx = 0
        events = signal_log.setdefault(name, [])
        # Skip past events before ``cursor``
        while idx < len(events) and events[idx].get("ts", 0) <= cursor:
            idx += 1

        while not await request.is_disconnected():
            events = signal_log.get(name, [])
            while idx < len(events):
                evt = events[idx]
                idx += 1
                ts = evt.get("ts", 0)
                if ts > cursor:
                    cursor = ts
                yield f"data: {json.dumps(evt)}\n\n"
            await asyncio.sleep(0.1)

    logger.debug("Starting signal stream for %s after %s", name, after)
    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    app.settings.host = "0.0.0.0"
    app.settings.port = int(os.environ.get("MCP_PORT", "8080"))
    logger.info("Starting MCP server on %s:%s", app.settings.host, app.settings.port)
    app.run(transport="streamable-http")
