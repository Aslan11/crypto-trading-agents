"""Execution agent workflow for receiving nudge signals."""

from __future__ import annotations

from typing import Dict, List, Any
from temporalio import workflow
from datetime import datetime, timezone


@workflow.defn
class ExecutionAgentWorkflow:
    """Workflow that receives nudge signals for the execution agent."""

    def __init__(self) -> None:
        self.nudges: list[int] = []
        self.user_preferences: Dict = {}
        self.log_entries: List[Dict[str, Any]] = []
        self.decision_count = 0
        self.action_count = 0
        self.summary_count = 0

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

    def _get_timestamp(self) -> Dict[str, Any]:
        """Get standardized timestamp information."""
        now = datetime.now(timezone.utc)
        return {
            "timestamp": int(now.timestamp()),
            "iso_timestamp": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S.%f")[:-3]  # Include milliseconds
        }

    @workflow.signal
    def log_decision(self, log_data: Dict[str, Any]) -> None:
        """Log a comprehensive trading decision with all context."""
        log_entry = {
            **self._get_timestamp(),
            "event_type": "decision",
            "entry_id": f"decision_{self.decision_count + 1}",
            **log_data
        }
        
        self.log_entries.append(log_entry)
        self.decision_count += 1

    @workflow.signal
    def log_action(self, log_data: Dict[str, Any]) -> None:
        """Log a specific action taken by the agent."""
        log_entry = {
            **self._get_timestamp(),
            "event_type": "action",
            "entry_id": f"action_{self.action_count + 1}",
            **log_data
        }
        
        self.log_entries.append(log_entry)
        self.action_count += 1

    @workflow.signal
    def log_summary(self, log_data: Dict[str, Any]) -> None:
        """Log summary information (evaluations, performance reports, etc.)."""
        log_entry = {
            **self._get_timestamp(),
            "event_type": "summary",
            "entry_id": f"summary_{self.summary_count + 1}",
            **log_data
        }
        
        self.log_entries.append(log_entry)
        self.summary_count += 1

    @workflow.query
    def get_logs(self, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Get log entries with optional filtering."""
        if params is None:
            params = {}
        
        event_type_filter = params.get("event_type")
        since_ts = params.get("since_ts", 0)
        limit = params.get("limit", 1000)
        
        # Filter logs
        filtered_logs = []
        for entry in self.log_entries:
            # Filter by timestamp
            if entry.get("timestamp", 0) < since_ts:
                continue
                
            # Filter by event type
            if event_type_filter and entry.get("event_type") != event_type_filter:
                continue
                
            filtered_logs.append(entry)
        
        # Sort by timestamp (newest first) and limit
        filtered_logs.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return filtered_logs[:limit]

    @workflow.query
    def get_stats(self) -> Dict[str, Any]:
        """Get logging statistics."""
        return {
            "total_entries": len(self.log_entries),
            "decision_count": self.decision_count,
            "action_count": self.action_count,
            "summary_count": self.summary_count,
            "agent": "execution_agent"
        }

    @workflow.query
    def get_recent_decisions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent decisions for analysis."""
        return self.get_logs({
            "event_type": "decision",
            "limit": limit
        })

    @workflow.query
    def get_recent_actions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent actions for analysis."""
        return self.get_logs({
            "event_type": "action", 
            "limit": limit
        })

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)