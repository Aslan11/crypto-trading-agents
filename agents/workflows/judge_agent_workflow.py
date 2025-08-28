"""Judge agent workflow for managing performance evaluations and prompt updates."""

from __future__ import annotations

from typing import Dict, List, Any
from datetime import datetime, timezone
from temporalio import workflow
import logging

logger = logging.getLogger(__name__)


@workflow.defn
class JudgeAgentWorkflow:
    """Workflow for managing performance evaluations and prompt updates."""

    def __init__(self) -> None:
        self.evaluations: List[Dict] = []
        self.context_history: List[Dict] = []
        # Initialize with defaults - will be updated from user preferences in run()
        self.current_context: Dict = {
            "risk_mode": "moderate",
            "performance_trend": ["stable"]
        }
        self.last_evaluation_ts: int = 0
        self.user_preferences: Dict = {}  # Store user preferences for risk baseline
        # Logging state
        self.log_entries: List[Dict[str, Any]] = []
        self.decision_count = 0
        self.action_count = 0
        self.summary_count = 0
        self.user_feedback: List[Dict[str, Any]] = []  # Store user feedback messages

    @workflow.signal
    def record_evaluation(self, evaluation: Dict) -> None:
        """Record a new performance evaluation."""
        evaluation["timestamp"] = int(datetime.now(timezone.utc).timestamp())
        evaluation["evaluation_id"] = len(self.evaluations) + 1
        self.evaluations.append(evaluation)
        self.last_evaluation_ts = evaluation["timestamp"]

    @workflow.signal  
    def set_user_preferences(self, preferences: Dict) -> None:
        """Update user trading preferences."""
        self.user_preferences.update(preferences)
        # Note: workflow.logger() should be used instead of regular logger in workflows
        workflow.logger.info("Judge agent received user preferences: risk_tolerance=%s, position_comfort=%s, cash_reserve=%s", 
                           preferences.get('risk_tolerance', 'unknown'),
                           preferences.get('position_size_comfort', 'unknown'), 
                           preferences.get('cash_reserve_level', 'unknown'))

    @workflow.signal
    def update_agent_context(self, context_data: Dict) -> None:
        """Update the agent's context."""
        # Update current context
        self.current_context.update(context_data.get("context", {}))
        
        # Record the context change
        context_record = {
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "context": self.current_context.copy(),
            "description": context_data.get("description", "Context update"),
            "reason": context_data.get("reason", "Performance-based adjustment"),
            "changes": context_data.get("changes", [])
        }
        
        self.context_history.append(context_record)

    @workflow.signal
    def rollback_context(self, target_index: int) -> None:
        """Rollback to a previous context."""
        if 0 <= target_index < len(self.context_history):
            target_context = self.context_history[target_index]
            self.current_context = target_context["context"].copy()

    @workflow.signal
    def trigger_immediate_evaluation(self, trigger_data: Dict) -> None:
        """Signal to trigger immediate evaluation (handled by judge agent client)."""
        evaluation_request = {
            "type": "immediate_trigger",
            "window_days": trigger_data.get("window_days", 7),
            "forced": trigger_data.get("forced", False),
            "timestamp": trigger_data.get("trigger_timestamp", int(datetime.now(timezone.utc).timestamp())),
            "overall_score": 0.0,
            "status": "trigger_requested"
        }
        self.evaluations.append(evaluation_request)

    @workflow.signal
    def mark_triggers_processed(self, data: Dict) -> None:
        """Mark any pending trigger requests as processed."""
        for evaluation in self.evaluations:
            if (evaluation.get("type") == "immediate_trigger" and 
                evaluation.get("status") == "trigger_requested"):
                evaluation["status"] = "processed"

    @workflow.query
    def get_evaluations(self, params: Dict = None) -> List[Dict]:
        """Get recent evaluations."""
        if params is None:
            params = {}
        
        limit = params.get("limit", 50)
        since_ts = params.get("since_ts", 0)
        
        filtered = [
            eval for eval in self.evaluations 
            if eval.get("timestamp", 0) >= since_ts
        ]
        # Return most recent first
        filtered.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return filtered[:limit]

    @workflow.query
    def get_context_history(self, limit: int = 20) -> List[Dict]:
        """Get context change history."""
        # Return most recent first
        history = sorted(self.context_history, key=lambda x: x["timestamp"], reverse=True)
        return history[:limit]

    @workflow.query
    def get_current_context(self) -> Dict:
        """Get the current agent context."""
        return self.current_context.copy()

    @workflow.query
    def get_performance_trend(self, days: int = 30) -> Dict:
        """Get performance trend over specified period."""
        cutoff_ts = int(datetime.now(timezone.utc).timestamp()) - (days * 24 * 60 * 60)
        recent_evaluations = [
            eval for eval in self.evaluations 
            if eval["timestamp"] >= cutoff_ts
        ]
        
        if not recent_evaluations:
            return {
                "trend": "stable",
                "avg_score": 0.0,
                "evaluations_count": 0,
                "improvement": 0.0
            }
        
        # Sort by timestamp
        recent_evaluations.sort(key=lambda x: x["timestamp"])
        
        scores = [eval.get("overall_score", 0.0) for eval in recent_evaluations]
        avg_score = sum(scores) / len(scores)
        
        # Calculate trend
        if len(scores) >= 2:
            first_half = scores[:len(scores)//2]
            second_half = scores[len(scores)//2:]
            
            first_avg = sum(first_half) / len(first_half)
            second_avg = sum(second_half) / len(second_half)
            
            improvement = second_avg - first_avg
            
            if improvement > 5:
                trend = "improving"
            elif improvement < -5:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"
            improvement = 0.0
        
        return {
            "trend": trend,
            "avg_score": avg_score,
            "evaluations_count": len(recent_evaluations),
            "improvement": improvement
        }

    @workflow.query
    def should_trigger_evaluation(self, cooldown_hours: int = 4) -> bool:
        """Check if enough time has passed for a new evaluation."""
        current_ts = int(datetime.now(timezone.utc).timestamp())
        time_since_last = current_ts - self.last_evaluation_ts
        cooldown_seconds = cooldown_hours * 60 * 60
        
        return time_since_last >= cooldown_seconds

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
    def get_log_stats(self) -> Dict[str, Any]:
        """Get logging statistics."""
        return {
            "total_entries": len(self.log_entries),
            "decision_count": self.decision_count,
            "action_count": self.action_count,
            "summary_count": self.summary_count,
            "agent": "judge_agent"
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
    
    @workflow.signal
    def add_user_feedback(self, feedback_data: Dict[str, Any]) -> None:
        """Add user feedback to be incorporated into the judge's evaluation."""
        feedback_entry = {
            **self._get_timestamp(),
            "feedback_id": f"feedback_{len(self.user_feedback) + 1}",
            "message": feedback_data.get("message", ""),
            "source": feedback_data.get("source", "user"),
            "processed": False
        }
        self.user_feedback.append(feedback_entry)
        workflow.logger.info(f"Judge received user feedback: {feedback_data.get('message', '')[:100]}...")
    
    @workflow.query
    def get_pending_feedback(self) -> List[Dict[str, Any]]:
        """Get unprocessed user feedback."""
        return [fb for fb in self.user_feedback if not fb.get("processed", False)]
    
    @workflow.signal
    def mark_feedback_processed(self, feedback_id: str) -> None:
        """Mark a feedback message as processed."""
        for feedback in self.user_feedback:
            if feedback.get("feedback_id") == feedback_id:
                feedback["processed"] = True
                break

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)