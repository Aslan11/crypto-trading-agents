"""Minimal MCP server exposing Temporal workflows over HTTP."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Iterable
import sys

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
from agents.utils import format_log
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from datetime import datetime
from temporalio.client import Client, WorkflowExecutionStatus
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Ensure project root is on sys.path so ``tools`` can be imported regardless of
# current working directory.
root_dir = Path(__file__).resolve().parents[1]
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

# Global Temporal client and workflow registry
client: Client | None = None
workflows: dict[str, Callable[..., Any]]
signal_log: dict[str, list[dict]] = {}
_client_lock = asyncio.Lock()


async def get_temporal_client() -> Client:
    """Return connected Temporal client, initializing if necessary."""
    global client
    if client is None:
        async with _client_lock:
            if client is None:
                address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
                namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
                logger.info(
                    "Connecting to Temporal at %s (namespace=%s)", address, namespace
                )
                client = await Client.connect(address, namespace=namespace)
    return client


def _import_workflow_modules() -> Iterable[Any]:
    """Import all modules under ``tools`` and yield them."""
    import tools

    yield tools
    for _, name, _ in pkgutil.walk_packages(tools.__path__, prefix="tools."):
        module = importlib.import_module(name)
        yield module


def _discover_workflows() -> dict[str, Callable[..., Any]]:
    """Return mapping of workflow name to workflow callable."""
    modules = list(_import_workflow_modules())
    wf_map: dict[str, Callable[..., Any]] = {}
    for module in modules:
        for obj in module.__dict__.values():
            if hasattr(obj, "__temporal_workflow_definition"):
                name = obj.__temporal_workflow_definition.name
                wf_map[name] = obj
    return wf_map


def _resolve_workflow(name: str) -> Callable[..., Any]:
    """Return workflow callable for ``name`` or raise 404."""
    wf = _discover_workflows().get(name)
    if wf is None:
        logger.error("Unknown tool requested: %s", name)
        raise HTTPException(status_code=404, detail="Unknown tool")
    return wf


def _prepare_args(wf: Callable[..., Any], payload: dict[str, Any]) -> list[Any]:
    """Prepare positional args for workflow from payload."""
    sig = inspect.signature(wf.run if hasattr(wf, "run") else wf)
    params = list(sig.parameters.values())
    if hasattr(wf, "run") and params and params[0].name == "self":
        params = params[1:]
    args: list[Any] = []
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.name in payload:
            args.append(payload[p.name])
        elif p.default is not inspect._empty:
            args.append(p.default)
        else:
            raise ValueError(f"Missing parameter: {p.name}")
    return args


@asynccontextmanager
async def lifespan(app: FastMCP):
    global workflows, client
    await get_temporal_client()
    workflows = _discover_workflows()
    logger.info("Discovered %d workflows", len(workflows))
    try:
        yield
    finally:
        if client is not None:
            await client.close()
            client = None


app = FastMCP(lifespan=lifespan)


class HealthResponse(BaseModel):
    status: str


@app.custom_route("/healthz", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse(HealthResponse(status="ok").model_dump())


class StartWorkflowResponse(BaseModel):
    workflow_id: str
    run_id: str


@app.custom_route("/tools/{tool_name}", methods=["POST"])
async def start_tool(request: Request) -> Response:
    body = await request.json()
    tool_name = request.path_params["tool_name"]
    logger.info(
        "Request to start tool %s with payload:\n%s",
        tool_name,
        format_log(body),
    )
    wf = _resolve_workflow(tool_name)
    try:
        args = _prepare_args(wf, body if isinstance(body, dict) else {})
    except Exception as exc:
        logger.error("Invalid payload for %s: %s", tool_name, exc)
        return JSONResponse({"detail": str(exc)}, status_code=422)
    workflow_id = f"{tool_name}-{secrets.token_hex(8)}"
    logger.info(
        "Starting workflow %s with args:\n%s",
        workflow_id,
        format_log(args),
    )
    temporal_client = await get_temporal_client()
    handle = await temporal_client.start_workflow(
        wf.run if hasattr(wf, "run") else wf,
        args=args,
        id=workflow_id,
        task_queue="mcp-tools",
    )
    run_id = handle.result_run_id or handle.run_id
    logger.info("Workflow started: id=%s run_id=%s", workflow_id, run_id)
    return JSONResponse(
        StartWorkflowResponse(workflow_id=workflow_id, run_id=run_id).model_dump(),
        status_code=202,
    )


class WorkflowStatusResponse(BaseModel):
    status: str
    result: Any | None = None


@app.custom_route("/workflow/{workflow_id}/{run_id}", methods=["GET"])
async def workflow_status(request: Request) -> Response:
    workflow_id = request.path_params["workflow_id"]
    run_id = request.path_params["run_id"]
    logger.info("Fetching status for workflow %s run %s", workflow_id, run_id)
    temporal_client = await get_temporal_client()
    handle = temporal_client.get_workflow_handle(workflow_id, run_id=run_id)
    desc = await handle.describe()
    status_name = desc.status.name if desc.status else "UNKNOWN"
    logger.info("Workflow %s is %s", workflow_id, status_name)
    result: Any | None = None
    if desc.status and desc.status != WorkflowExecutionStatus.RUNNING:
        try:
            result = await handle.result()
        except Exception as exc:
            result = {"error": str(exc)}
    return JSONResponse(WorkflowStatusResponse(status=status_name, result=result).model_dump())


@app.custom_route("/signal/{name}", methods=["POST"])
async def record_signal(request: Request) -> Response:
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
    name = request.path_params["name"]
    after = int(request.query_params.get("after", "0"))
    events = [e for e in signal_log.get(name, []) if e.get("ts", 0) > after]
    return JSONResponse(events)


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))

    # FastMCP's ``run`` no longer accepts ``host`` or ``port`` parameters.
    # Configure the server via ``settings`` instead and explicitly use the
    # Streamable HTTP transport so our custom routes are served over HTTP.
    app.settings.host = host
    app.settings.port = port
    app.run(transport="streamable-http")
