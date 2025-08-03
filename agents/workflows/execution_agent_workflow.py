"""Execution agent workflow for receiving nudge signals."""

from __future__ import annotations

from temporalio import workflow


@workflow.defn
class ExecutionAgentWorkflow:
    """Workflow that receives nudge signals for the execution agent."""

    def __init__(self) -> None:
        self.nudges: list[int] = []

    @workflow.signal
    def nudge(self, ts: int) -> None:
        self.nudges.append(ts)

    @workflow.query
    def get_nudges(self) -> list[int]:
        return list(self.nudges)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)