from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
import secrets
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterable

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from temporalio.client import Client, WorkflowExecutionStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Global Temporal client and workflow registry
client: Client
workflows: dict[str, Callable[..., Any]]


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
    global client, workflows
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    logger.info("Connecting to Temporal at %s (namespace=%s)", address, namespace)
    client = await Client.connect(address, namespace=namespace)
    workflows = _discover_workflows()
    logger.info("Discovered %d workflows", len(workflows))
    try:
        yield
    finally:
        await client.close()


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
    logger.info("Request to start tool %s with payload %s", tool_name, body)
    wf = workflows.get(tool_name)
    if not wf:
        logger.error("Unknown tool requested: %s", tool_name)
        return JSONResponse({"detail": "Unknown tool"}, status_code=404)
    try:
        args = _prepare_args(wf, body if isinstance(body, dict) else {})
    except Exception as exc:
        logger.error("Invalid payload for %s: %s", tool_name, exc)
        return JSONResponse({"detail": str(exc)}, status_code=422)
    workflow_id = f"{tool_name}-{secrets.token_hex(8)}"
    logger.info("Starting workflow %s with args %s", workflow_id, args)
    handle = await client.start_workflow(
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
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
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


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    app.run(host=host, port=port)
