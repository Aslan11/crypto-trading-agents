"""MCP server exposing Temporal workflows as tools."""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any, Dict, List
from datetime import datetime
import json

from mcp.server.fastmcp import FastMCP
import logging
from temporalio.client import Client, RPCError, RPCStatusCode, WorkflowExecutionStatus
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import Request

# Import workflow classes
from tools.market_data import SubscribeCEXStream
from tools.strategy_signal import EvaluateStrategyMomentum
from tools.risk import PreTradeRiskCheck
from tools.execution import PlaceMockOrder
from tools.wallet import SignAndSendTx
from agents.workflows import ExecutionLedgerWorkflow

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
    """Start a durable workflow to stream market data from a CEX."""
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
    logger.info("Workflow %s started run %s", workflow_id, handle.run_id)
    return {"workflow_id": workflow_id, "run_id": handle.run_id}


@app.tool(annotations={"title": "Start Market Stream", "readOnlyHint": True})
async def start_market_stream(
    symbols: List[str], interval_sec: int = 1
) -> Dict[str, str]:
    """Convenience wrapper around ``subscribe_cex_stream``."""
    return await subscribe_cex_stream(symbols, interval_sec)


@app.tool(annotations={"title": "Evaluate Momentum Strategy", "readOnlyHint": True})
async def evaluate_strategy_momentum(
    signal: Dict[str, Any], cooldown_sec: int = 0
) -> Dict[str, Any]:
    """Invoke the momentum strategy evaluation workflow."""
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


@app.tool(annotations={"title": "Pre-Trade Risk Check", "readOnlyHint": True})
async def pre_trade_risk_check(
    intent_id: str, intents: List[Dict[str, Any]]
) -> Dict[str, str]:
    """Run a pre-trade risk check on proposed order intents."""
    client = await get_temporal_client()
    workflow_id = f"risk-{intent_id}"
    logger.info("Starting PreTradeRiskCheck %s for %d intents", intent_id, len(intents))
    handle = await client.start_workflow(
        PreTradeRiskCheck.run,
        args=[intent_id, intents],
        id=workflow_id,
        task_queue="mcp-tools",
    )
    result: Dict[str, str] = await handle.result()
    logger.info("Risk check %s completed with %s", workflow_id, result.get("status"))
    return result


@app.tool(
    annotations={
        "title": "Place Mock Order",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
async def place_mock_order(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Simulate executing an order intent."""
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
        ledger = client.get_workflow_handle(
            os.environ.get("LEDGER_WF_ID", "mock-ledger")
        )
        await ledger.signal("record_fill", fill)
    except Exception:
        pass
    return fill


@app.tool(
    annotations={
        "title": "Sign and Send Transaction",
        "readOnlyHint": False,
        "openWorldHint": True,
    }
)
async def sign_and_send_tx(
    raw_tx: Dict[str, Any], wallet_label: str, rpc_url: str
) -> Dict[str, str]:
    """Sign an EVM transaction and broadcast it."""
    client = await get_temporal_client()
    workflow_id = f"tx-{secrets.token_hex(4)}"
    logger.info("Signing and sending tx via workflow %s", workflow_id)
    handle = await client.start_workflow(
        SignAndSendTx.run,
        args=[raw_tx, wallet_label, rpc_url],
        id=workflow_id,
        task_queue="mcp-tools",
    )
    result: Dict[str, str] = await handle.result()
    logger.info("Tx workflow %s completed", workflow_id)
    return result


@app.tool(annotations={"title": "Get Historical Ticks", "readOnlyHint": True})
async def get_historical_ticks(symbol: str, days: int | None = None) -> List[Dict[str, float]]:
    """Return historical ticks for ``symbol``.

    This function queries the ``feature-<symbol>`` workflow for stored ticks.
    If the workflow does not exist or has not yet recorded any data, the
    fallback is to use ticks from the server's in-memory ``signal_log`` so the
    tool still returns useful information.

    Parameters
    ----------
    symbol:
        Asset pair in ``BASE/QUOTE`` format.
    days:
        Number of days of history requested. ``None`` (default) returns **all**
        stored ticks.
    """

    cutoff = 0 if days is None else int(datetime.utcnow().timestamp()) - days * 86400
    client = await get_temporal_client()
    wf_id = f"feature-{symbol.replace('/', '-')}"
    handle = client.get_workflow_handle(wf_id)
    ticks: dict[int, float] = {}
    try:
        result = await handle.query("historical_ticks", cutoff)
        for t in result:
            ticks[int(t["ts"])] = float(t["price"])
        logger.info("Fetched %d ticks for %s from workflow", len(ticks), symbol)
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            logger.info("Feature workflow %s not found; using signal log", wf_id)
        else:
            raise

    if not ticks:
        events = signal_log.get("market_tick", [])
        for e in events:
            if e.get("symbol") != symbol or e.get("ts", 0) < cutoff:
                continue
            data = e.get("data", {})
            if "last" in data:
                price = float(data["last"])
            elif {"bid", "ask"}.issubset(data):
                price = (float(data["bid"]) + float(data["ask"])) / 2
            else:
                continue
            ticks[int(e["ts"])] = price
        logger.info("Fetched %d ticks for %s from signal log", len(ticks), symbol)

    all_ticks = [{"ts": ts, "price": price} for ts, price in sorted(ticks.items())]
    logger.info("Returning %d ticks for %s", len(all_ticks), symbol)
    return all_ticks


@app.tool(annotations={"title": "Get Portfolio Status", "readOnlyHint": True})
async def get_portfolio_status() -> Dict[str, Any]:
    """Retrieve current portfolio cash and positions from the ledger."""
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
        ts = int(datetime.utcnow().timestamp())
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
