"""MCP server exposing Temporal workflows as tools."""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any, Dict, List
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from temporalio.client import Client, RPCError, RPCStatusCode, WorkflowExecutionStatus
from starlette.responses import JSONResponse, Response
from starlette.requests import Request

# Import workflow classes
from tools.market_data import SubscribeCEXStream
from tools.strategy_signal import EvaluateStrategyMomentum
from tools.risk import PreTradeRiskCheck
from tools.execution import PlaceMockOrder
from tools.wallet import SignAndSendTx
from agents.workflows import ExecutionLedgerWorkflow

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
                _temporal_client = await Client.connect(address, namespace=namespace)
    return _temporal_client


@app.tool(annotations={"title": "Subscribe CEX Stream", "readOnlyHint": True})
async def subscribe_cex_stream(exchange: str, symbols: List[str], interval_sec: int = 1) -> Dict[str, str]:
    """Start a durable workflow to stream market data from a CEX."""
    client = await get_temporal_client()
    workflow_id = f"stream-{secrets.token_hex(4)}"
    handle = await client.start_workflow(
        SubscribeCEXStream.run,
        exchange,
        symbols,
        interval_sec,
        id=workflow_id,
        task_queue="mcp-tools",
    )
    return {"workflow_id": workflow_id, "run_id": handle.run_id}


@app.tool(annotations={"title": "Evaluate Momentum Strategy", "readOnlyHint": True})
async def evaluate_strategy_momentum(signal: Dict[str, Any], cooldown_sec: int = 0) -> Dict[str, Any]:
    """Invoke the momentum strategy evaluation workflow."""
    client = await get_temporal_client()
    workflow_id = f"momentum-{secrets.token_hex(4)}"
    handle = await client.start_workflow(
        EvaluateStrategyMomentum.run,
        signal,
        cooldown_sec or None,
        id=workflow_id,
        task_queue="mcp-tools",
    )
    result = await handle.result()
    return result


@app.tool(annotations={"title": "Pre-Trade Risk Check", "readOnlyHint": True})
async def pre_trade_risk_check(intent_id: str, intents: List[Dict[str, Any]]) -> Dict[str, str]:
    """Run a pre-trade risk check on proposed order intents."""
    client = await get_temporal_client()
    workflow_id = f"risk-{intent_id}"
    handle = await client.start_workflow(
        PreTradeRiskCheck.run,
        intent_id,
        intents,
        id=workflow_id,
        task_queue="mcp-tools",
    )
    result: Dict[str, str] = await handle.result()
    return result


@app.tool(annotations={"title": "Place Mock Order", "readOnlyHint": False, "destructiveHint": False})
async def place_mock_order(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Simulate executing an order intent."""
    client = await get_temporal_client()
    workflow_id = f"order-{secrets.token_hex(4)}"
    handle = await client.start_workflow(
        PlaceMockOrder.run,
        intent,
        id=workflow_id,
        task_queue="mcp-tools",
    )
    fill = await handle.result()
    try:
        ledger = client.get_workflow_handle(os.environ.get("LEDGER_WF_ID", "mock-ledger"))
        await ledger.signal("record_fill", fill)
    except Exception:
        pass
    return fill


@app.tool(annotations={"title": "Sign and Send Transaction", "readOnlyHint": False, "openWorldHint": True})
async def sign_and_send_tx(raw_tx: Dict[str, Any], wallet_label: str, rpc_url: str) -> Dict[str, str]:
    """Sign an EVM transaction and broadcast it."""
    client = await get_temporal_client()
    workflow_id = f"tx-{secrets.token_hex(4)}"
    handle = await client.start_workflow(
        SignAndSendTx.run,
        raw_tx,
        wallet_label,
        rpc_url,
        id=workflow_id,
        task_queue="mcp-tools",
    )
    result: Dict[str, str] = await handle.result()
    return result


@app.tool(annotations={"title": "Get Portfolio Status", "readOnlyHint": True})
async def get_portfolio_status() -> Dict[str, Any]:
    """Retrieve current portfolio cash and positions from the ledger."""
    client = await get_temporal_client()
    wf_id = os.environ.get("LEDGER_WF_ID", "mock-ledger")
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
    return {"cash": cash, "positions": positions, "entry_prices": entry_prices}


@app.custom_route("/workflow/{workflow_id}/{run_id}", methods=["GET"])
async def workflow_status(request: Request) -> Response:
    workflow_id = request.path_params["workflow_id"]
    run_id = request.path_params["run_id"]
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
    return JSONResponse({"status": status_name, "result": result})


@app.custom_route("/signal/{name}", methods=["POST"])
async def record_signal(request: Request) -> Response:
    """Record a signal event for services still using HTTP polling."""
    name = request.path_params["name"]
    payload = await request.json()
    ts = payload.get("ts")
    if ts is None:
        ts = int(datetime.utcnow().timestamp())
        payload["ts"] = ts
    signal_log.setdefault(name, []).append(payload)
    return Response(status_code=204)


@app.custom_route("/signal/{name}", methods=["GET"])
async def fetch_signals(request: Request) -> Response:
    """Return signals newer than the provided 'after' timestamp."""
    name = request.path_params["name"]
    after = int(request.query_params.get("after", "0"))
    events = [e for e in signal_log.get(name, []) if e.get("ts", 0) > after]
    return JSONResponse(events)


if __name__ == "__main__":
    app.settings.host = "0.0.0.0"
    app.settings.port = int(os.environ.get("MCP_PORT", "8080"))
    app.run(transport="streamable-http")
