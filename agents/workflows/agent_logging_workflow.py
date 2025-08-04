"""Agent logging workflow for centralized decision and action tracking."""

from __future__ import annotations

from typing import Dict, List, Any
from datetime import datetime, timezone
from temporalio import workflow


@workflow.defn
class AgentLoggingWorkflow:
    """Centralized logging workflow for all agent decisions, actions, and summaries."""

    def __init__(self) -> None:
        self.log_entries: List[Dict[str, Any]] = []
        self.decision_count = 0
        self.action_count = 0
        self.summary_count = 0

    @workflow.signal
    def log_decision(
        self, 
        agent_name: str,
        nudge_timestamp: int,
        symbols: List[str],
        market_data_summary: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        user_preferences: Dict[str, Any],
        decisions: Dict[str, Any],
        reasoning: str,
        extra_data: Dict[str, Any] = None
    ) -> None:
        """Log a comprehensive trading decision with all context."""
        log_entry = {
            **self._get_timestamp(),
            "agent": agent_name,
            "event_type": "decision",
            "entry_id": f"decision_{self.decision_count + 1}",
            "nudge_timestamp": nudge_timestamp,
            "symbols": symbols,
            "market_data_summary": market_data_summary,
            "portfolio_data": portfolio_data,
            "user_preferences": user_preferences,
            "decisions": decisions,
            "reasoning": reasoning,
            **(extra_data or {})
        }
        
        self.log_entries.append(log_entry)
        self.decision_count += 1

    @workflow.signal
    def log_action(
        self,
        agent_name: str,
        action_type: str,
        details: Dict[str, Any],
        result: Dict[str, Any] = None,
        extra_data: Dict[str, Any] = None
    ) -> None:
        """Log a specific action taken by the agent."""
        log_entry = {
            **self._get_timestamp(),
            "agent": agent_name,
            "event_type": "action",
            "entry_id": f"action_{self.action_count + 1}",
            "action_type": action_type,
            "details": details,
            "result": result or {},
            **(extra_data or {})
        }
        
        self.log_entries.append(log_entry)
        self.action_count += 1

    @workflow.signal
    def log_summary(
        self,
        agent_name: str,
        summary_type: str,
        data: Dict[str, Any],
        extra_data: Dict[str, Any] = None
    ) -> None:
        """Log summary information (evaluations, performance reports, etc.)."""
        log_entry = {
            **self._get_timestamp(),
            "agent": agent_name,
            "event_type": "summary",
            "entry_id": f"summary_{self.summary_count + 1}",
            "summary_type": summary_type,
            "data": data,
            **(extra_data or {})
        }
        
        self.log_entries.append(log_entry)
        self.summary_count += 1

    def _get_timestamp(self) -> Dict[str, Any]:
        """Get standardized timestamp information."""
        now = datetime.now(timezone.utc)
        return {
            "timestamp": int(now.timestamp()),
            "iso_timestamp": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S.%f")[:-3]  # Include milliseconds
        }

    @workflow.query
    def get_logs(self, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Get log entries with optional filtering."""
        if params is None:
            params = {}
        
        agent_filter = params.get("agent")
        event_type_filter = params.get("event_type")
        since_ts = params.get("since_ts", 0)
        limit = params.get("limit", 1000)
        
        # Filter logs
        filtered_logs = []
        for entry in self.log_entries:
            # Filter by timestamp
            if entry.get("timestamp", 0) < since_ts:
                continue
                
            # Filter by agent
            if agent_filter and entry.get("agent") != agent_filter:
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
            "agents": list(set(entry.get("agent", "unknown") for entry in self.log_entries))
        }

    @workflow.query
    def get_recent_decisions(self, agent_name: str = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent decisions for analysis."""
        return self.get_logs({
            "agent": agent_name,
            "event_type": "decision",
            "limit": limit
        })

    @workflow.query
    def get_recent_actions(self, agent_name: str = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent actions for analysis."""
        return self.get_logs({
            "agent": agent_name,
            "event_type": "action", 
            "limit": limit
        })

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)