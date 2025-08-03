"""Judge agent workflow for managing performance evaluations and prompt updates."""

from __future__ import annotations

from typing import Dict, List
from datetime import datetime, timezone
from temporalio import workflow


@workflow.defn
class JudgeAgentWorkflow:
    """Workflow for managing performance evaluations and prompt updates."""

    def __init__(self) -> None:
        self.evaluations: List[Dict] = []
        self.prompt_versions: List[Dict] = []
        self.current_prompt_version: int = 1
        self.last_evaluation_ts: int = 0
        
        # Initialize with base prompt version
        self.prompt_versions.append({
            "version": 1,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "prompt_type": "execution_agent",
            "description": "Initial system prompt",
            "performance_score": 0.0,
            "is_active": True
        })

    @workflow.signal
    def record_evaluation(self, evaluation: Dict) -> None:
        """Record a new performance evaluation."""
        evaluation["timestamp"] = int(datetime.now(timezone.utc).timestamp())
        evaluation["evaluation_id"] = len(self.evaluations) + 1
        self.evaluations.append(evaluation)
        self.last_evaluation_ts = evaluation["timestamp"]

    @workflow.signal
    def update_prompt_version(self, prompt_data: Dict) -> None:
        """Record a new prompt version."""
        # Deactivate current active version
        for version in self.prompt_versions:
            if version["is_active"]:
                version["is_active"] = False
        
        # Add new version
        new_version = {
            "version": len(self.prompt_versions) + 1,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "prompt_type": prompt_data.get("prompt_type", "execution_agent"),
            "prompt_content": prompt_data.get("prompt_content", ""),
            "description": prompt_data.get("description", ""),
            "changes": prompt_data.get("changes", []),
            "reason": prompt_data.get("reason", ""),
            "performance_score": 0.0,
            "is_active": True
        }
        
        self.prompt_versions.append(new_version)
        self.current_prompt_version = new_version["version"]

    @workflow.signal
    def rollback_prompt(self, target_version: int) -> None:
        """Rollback to a previous prompt version."""
        target_prompt = None
        for version in self.prompt_versions:
            version["is_active"] = False
            if version["version"] == target_version:
                target_prompt = version
        
        if target_prompt:
            target_prompt["is_active"] = True
            self.current_prompt_version = target_version

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
    def get_prompt_versions(self, limit: int = 20) -> List[Dict]:
        """Get prompt version history."""
        # Return most recent first
        versions = sorted(self.prompt_versions, key=lambda x: x["timestamp"], reverse=True)
        return versions[:limit]

    @workflow.query
    def get_current_prompt_version(self) -> Dict:
        """Get the currently active prompt version."""
        for version in self.prompt_versions:
            if version["is_active"]:
                return version
        # Fallback to latest version
        return self.prompt_versions[-1] if self.prompt_versions else {}

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

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)