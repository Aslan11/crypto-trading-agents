"""Execution agent workflow for receiving nudge signals."""

from __future__ import annotations

from typing import Dict
from temporalio import workflow


@workflow.defn
class ExecutionAgentWorkflow:
    """Workflow that receives nudge signals for the execution agent."""

    def __init__(self) -> None:
        self.nudges: list[int] = []
        self.user_preferences: Dict = {}

    @workflow.signal
    def nudge(self, ts: int) -> None:
        self.nudges.append(ts)

    @workflow.signal  
    def set_user_preferences(self, preferences: Dict) -> None:
        """Update user trading preferences."""
        self.user_preferences.update(preferences)

    @workflow.query
    def get_nudges(self) -> list[int]:
        return list(self.nudges)
        
    @workflow.query
    def get_user_preferences(self) -> Dict:
        """Get current user preferences."""
        return dict(self.user_preferences)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)