"""Broker agent workflow for storing active trading symbols and user preferences."""

from __future__ import annotations

from typing import Dict
from temporalio import workflow


@workflow.defn
class BrokerAgentWorkflow:
    """Workflow storing active trading symbols and user preferences for the broker agent."""

    def __init__(self) -> None:
        self.symbols: list[str] = []
        self.user_preferences: Dict = {
            "risk_tolerance": "moderate",  # conservative, moderate, aggressive
            "experience_level": "intermediate",  # beginner, intermediate, advanced
            "max_position_size": 0.20,  # Maximum % of portfolio per position
            "cash_reserve": 0.10,  # Minimum cash reserve %
            "max_drawdown_tolerance": 0.15,  # Maximum acceptable drawdown
            "trading_style": "balanced"  # conservative, balanced, aggressive
        }

    @workflow.signal
    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)

    @workflow.signal
    def set_user_preferences(self, preferences: Dict) -> None:
        """Update user trading preferences."""
        self.user_preferences.update(preferences)

    @workflow.query
    def get_symbols(self) -> list[str]:
        return list(self.symbols)

    @workflow.query
    def get_user_preferences(self) -> Dict:
        """Get current user trading preferences."""
        return dict(self.user_preferences)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)