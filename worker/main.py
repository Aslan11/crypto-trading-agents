#!/usr/bin/env python
"""Temporal worker entrypoint."""

from __future__ import annotations

import asyncio
import importlib
import os
import pkgutil
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker
# Import UnsandboxedWorkflowRunner from temporalio.worker to ensure
# compatibility with latest Temporal SDK versions where the class is exported
# directly from the worker package.
from temporalio.worker import UnsandboxedWorkflowRunner


def _add_project_root_to_path() -> None:
    """Add repository root directory to ``sys.path``."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _discover_modules() -> Iterable[Any]:
    """Yield all imported modules under ``tools``."""
    import tools

    # Include the base package itself
    yield tools

    for finder, name, ispkg in pkgutil.walk_packages(tools.__path__, prefix="tools."):
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - import errors should not kill worker
            print(f"Failed to import {name}: {exc}", file=sys.stderr)
            continue
        yield module


def _collect_definitions(modules: Iterable[Any]) -> tuple[Sequence[type], Sequence[Any]]:
    """Collect workflow and activity definitions from modules."""
    workflows: list[type] = []
    activities: list[Any] = []
    for module in modules:
        for obj in module.__dict__.values():
            if hasattr(obj, "__temporal_workflow_definition"):
                workflows.append(obj)
            elif hasattr(obj, "__temporal_activity_definition"):
                activities.append(obj)
    return workflows, activities


async def main() -> None:
    _add_project_root_to_path()

    modules = list(_discover_modules())
    workflows, activities = _collect_definitions(modules)

    print(f"Loaded {len(workflows)} workflows and {len(activities)} activities")

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(address, namespace=namespace)
    task_queue = os.environ.get("TASK_QUEUE", "mcp-tools")

    with ThreadPoolExecutor() as activity_executor:
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=workflows,
            activities=activities,
            activity_executor=activity_executor,
            workflow_runner=UnsandboxedWorkflowRunner(),
        )

        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
