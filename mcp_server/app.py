"""MCP server exposing Temporal workflows as tools."""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any, Dict, List
from datetime import datetime
import json

import logging
from temporalio.client import Client, RPCError, RPCStatusCode, WorkflowExecutionStatus

# Minimal placeholder App to mimic FastMCP for tests
class _Settings:
    streamable_http_path = ""
    json_response = True
    host = "localhost"
    port = 0


class App:
    def __init__(self, name: str):
        self.name = name
        self.settings = _Settings()

    def tool(self, **_):
        def decorator(fn):
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def streamable_http_app(self):
        return self

    def run(self, transport: str = "streamable-http") -> None:
        """Start a stub server that simply blocks forever."""
        print(
            f"Starting stub MCP server on {self.settings.host}:{self.settings.port} ({transport})"
        )
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

app = App("crypto-trading-server")

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
    """Return historical ticks for ``symbol`` fetched from its feature workflow.

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
    logger.info("Querying workflow %s for ticks >= %d", wf_id, cutoff)
    handle = client.get_workflow_handle(wf_id)
    try:
        result = await handle.query("historical_ticks", cutoff)
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            logger.warning("Feature workflow %s not found", wf_id)
            return []
        raise

    ticks = [
        {"ts": int(t["ts"]), "price": float(t["price"])}
        for t in result
    ]
    logger.info("Retrieved %d ticks for %s", len(ticks), symbol)
    return ticks


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


async def workflow_status(request) -> dict:
    workflow_id = request.path_params["workflow_id"]
    run_id = request.path_params["run_id"]
    logger.info("Fetching status for %s %s", workflow_id, run_id)
    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    desc = await handle.describe()
    status = desc.status
    if hasattr(status, "name"):
        status_name = status.name
    else:
        status_name = status or "UNKNOWN"
    result: Any | None = None
    if desc.status and desc.status != WorkflowExecutionStatus.RUNNING:
        try:
            result = await handle.result()
        except Exception as exc:
            result = {"error": str(exc)}
    logger.info("Workflow %s status %s", workflow_id, status_name)
    return {"status": status_name, "result": result}




if __name__ == "__main__":
    app.settings.host = "0.0.0.0"
    app.settings.port = int(os.environ.get("MCP_PORT", "8080"))
    logger.info("Starting MCP server on %s:%s", app.settings.host, app.settings.port)
    app.run(transport="streamable-http")
