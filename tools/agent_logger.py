"""Workflow-based logging system for agent decisions and actions."""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional
import logging
import glob
from temporalio.client import Client


class AgentLogger:
    """Workflow-based logging system for tracking agent decisions and performance."""
    
    def __init__(self, agent_name: str, temporal_client: Client = None, log_dir: str = "logs", days_to_keep: int = 30):
        self.agent_name = agent_name
        self.temporal_client = temporal_client
        self.log_dir = Path(log_dir)
        self.days_to_keep = days_to_keep
        
        # Create logs directory if it doesn't exist (for debug logs only)
        self.log_dir.mkdir(exist_ok=True)
        
        # Clean up old logs on initialization (debug logs only)
        self.cleanup_old_logs()
        
        # Set up Python logger for debug/error messages (still file-based)
        self.logger = logging.getLogger(f"AgentLogger.{agent_name}")
        handler = logging.FileHandler(self.log_dir / f"{agent_name}_debug.log")
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
    
    def _get_daily_log_path(self, log_type: str) -> Path:
        """Get the log path for today's date."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"{self.agent_name}_{log_type}_{date_str}.jsonl"
    
    def cleanup_old_logs(self) -> None:
        """Remove log files older than the specified number of days."""
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.days_to_keep)
            cutoff_str = cutoff_date.strftime("%Y-%m-%d")
            
            # Find all log files for this agent
            pattern = str(self.log_dir / f"{self.agent_name}_*_????-??-??.jsonl")
            old_files = []
            
            for file_path in glob.glob(pattern):
                filename = Path(file_path).name
                # Extract date from filename (assuming format: agent_type_YYYY-MM-DD.jsonl)
                try:
                    date_part = filename.split("_")[-1].replace(".jsonl", "")
                    if date_part < cutoff_str:
                        old_files.append(file_path)
                except (IndexError, ValueError):
                    # Skip files that don't match expected format
                    continue
            
            # Remove old files
            for file_path in old_files:
                try:
                    os.remove(file_path)
                    self.logger.info(f"Removed old log file: {file_path}")
                except OSError as e:
                    self.logger.error(f"Failed to remove {file_path}: {e}")
                    
        except Exception as e:
            self.logger.error(f"Failed to cleanup old logs: {e}")
    
    def _get_timestamp(self) -> Dict[str, Any]:
        """Get standardized timestamp information."""
        now = datetime.now(timezone.utc)
        return {
            "timestamp": int(now.timestamp()),
            "iso_timestamp": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S.%f")[:-3]  # Include milliseconds
        }
    
    def _get_workflow_id(self) -> str:
        """Get the appropriate workflow ID for this agent."""
        agent_workflow_map = {
            "execution_agent": "execution-agent",
            "judge_agent": "judge-agent",
            "broker_agent": "broker-agent"  # If we add logging to broker later
        }
        return agent_workflow_map.get(self.agent_name, "execution-agent")  # Default fallback

    async def log_decision(
        self, 
        nudge_timestamp: int,
        symbols: list[str],
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        user_preferences: Dict[str, Any],
        decisions: Dict[str, Any],
        reasoning: str,
        **kwargs
    ) -> None:
        """Log a comprehensive trading decision with all context."""
        if self.temporal_client:
            try:
                # Signal the appropriate agent workflow
                workflow_id = self._get_workflow_id()
                handle = self.temporal_client.get_workflow_handle(workflow_id)
                
                # Ensure data is dict type
                if not isinstance(market_data, dict):
                    market_data = {}
                if not isinstance(portfolio_data, dict):
                    portfolio_data = {}
                if not isinstance(user_preferences, dict):
                    user_preferences = {}
                
                # Prepare market data summary
                market_data_summary = {
                    "symbols_count": len(market_data.get("historical_ticks", {})),
                    "latest_prices": self._extract_latest_prices(market_data),
                    "data_timespan": self._calculate_data_timespan(market_data)
                }
                
                # Prepare portfolio data
                portfolio_summary = {
                    "cash": portfolio_data.get("cash"),
                    "total_pnl": portfolio_data.get("total_pnl"),
                    "realized_pnl": portfolio_data.get("realized_pnl"),
                    "unrealized_pnl": portfolio_data.get("unrealized_pnl"),
                    "positions_count": len(portfolio_data.get("positions", {})),
                    "positions": portfolio_data.get("positions", {})
                }
                
                # Pack all data into a single dictionary
                log_data = {
                    "agent": self.agent_name,
                    "nudge_timestamp": nudge_timestamp,
                    "symbols": symbols,
                    "market_data_summary": market_data_summary,
                    "portfolio_data": portfolio_summary,
                    "user_preferences": user_preferences,
                    "decisions": decisions,
                    "reasoning": reasoning,
                    **kwargs
                }
                
                await handle.signal("log_decision", log_data)
                self.logger.info(f"Decision logged to workflow for nudge {nudge_timestamp}")
            except Exception as exc:
                self.logger.error(f"Failed to log decision to workflow: {exc}")
                # Fallback to file logging
                self._log_decision_to_file(nudge_timestamp, symbols, market_data, portfolio_data, user_preferences, decisions, reasoning, **kwargs)
        else:
            # Fallback to file logging
            self._log_decision_to_file(nudge_timestamp, symbols, market_data, portfolio_data, user_preferences, decisions, reasoning, **kwargs)
    
    def _log_decision_to_file(
        self, 
        nudge_timestamp: int,
        symbols: list[str],
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        user_preferences: Dict[str, Any],
        decisions: Dict[str, Any],
        reasoning: str,
        **kwargs
    ) -> None:
        """Fallback file-based decision logging."""
        # Ensure data is dict type
        if not isinstance(market_data, dict):
            market_data = {}
        if not isinstance(portfolio_data, dict):
            portfolio_data = {}
        if not isinstance(user_preferences, dict):
            user_preferences = {}
            
        log_entry = {
            **self._get_timestamp(),
            "agent": self.agent_name,
            "event_type": "decision",
            "nudge_timestamp": nudge_timestamp,
            "symbols": symbols,
            "market_data_summary": {
                "symbols_count": len(market_data.get("historical_ticks", {})),
                "latest_prices": self._extract_latest_prices(market_data),
                "data_timespan": self._calculate_data_timespan(market_data)
            },
            "portfolio_summary": {
                "cash": portfolio_data.get("cash"),
                "total_pnl": portfolio_data.get("total_pnl"),
                "realized_pnl": portfolio_data.get("realized_pnl"),
                "unrealized_pnl": portfolio_data.get("unrealized_pnl"),
                "positions_count": len(portfolio_data.get("positions", {})),
                "positions": portfolio_data.get("positions", {})
            },
            "user_preferences": user_preferences,
            "decisions": decisions,
            "reasoning": reasoning,
            **kwargs
        }
        
        self._write_jsonl(self._get_daily_log_path("decisions"), log_entry)
        self.logger.info(f"Decision logged to file for nudge {nudge_timestamp}")
    
    async def log_action(
        self,
        action_type: str,
        details: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> None:
        """Log a specific action taken by the agent."""
        if self.temporal_client:
            try:
                # Signal the appropriate agent workflow
                workflow_id = self._get_workflow_id()
                handle = self.temporal_client.get_workflow_handle(workflow_id)
                # Pack all data into a single dictionary
                log_data = {
                    "agent": self.agent_name,
                    "action_type": action_type,
                    "details": details,
                    "result": result or {},
                    **kwargs
                }
                
                await handle.signal("log_action", log_data)
                self.logger.info(f"Action logged to workflow: {action_type}")
            except Exception as exc:
                self.logger.error(f"Failed to log action to workflow: {exc}")
                # Fallback to file logging
                self._log_action_to_file(action_type, details, result, **kwargs)
        else:
            # Fallback to file logging
            self._log_action_to_file(action_type, details, result, **kwargs)
    
    def _log_action_to_file(
        self,
        action_type: str,
        details: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> None:
        """Fallback file-based action logging."""
        log_entry = {
            **self._get_timestamp(),
            "agent": self.agent_name,
            "event_type": "action",
            "action_type": action_type,
            "details": details,
            "result": result,
            **kwargs
        }
        
        self._write_jsonl(self._get_daily_log_path("actions"), log_entry)
        self.logger.info(f"Action logged to file: {action_type}")
    
    async def log_summary(
        self,
        summary_type: str,
        data: Dict[str, Any],
        **kwargs
    ) -> None:
        """Log summary information (evaluations, performance reports, etc.)."""
        if self.temporal_client:
            try:
                # Signal the appropriate agent workflow
                workflow_id = self._get_workflow_id()
                handle = self.temporal_client.get_workflow_handle(workflow_id)
                # Pack all data into a single dictionary
                log_data = {
                    "agent": self.agent_name,
                    "summary_type": summary_type,
                    "data": data,
                    **kwargs
                }
                
                await handle.signal("log_summary", log_data)
                self.logger.info(f"Summary logged to workflow: {summary_type}")
            except Exception as exc:
                self.logger.error(f"Failed to log summary to workflow: {exc}")
                # Fallback to file logging
                self._log_summary_to_file(summary_type, data, **kwargs)
        else:
            # Fallback to file logging
            self._log_summary_to_file(summary_type, data, **kwargs)
    
    def _log_summary_to_file(
        self,
        summary_type: str,
        data: Dict[str, Any],
        **kwargs
    ) -> None:
        """Fallback file-based summary logging."""
        log_entry = {
            **self._get_timestamp(),
            "agent": self.agent_name,
            "event_type": "summary",
            "summary_type": summary_type,
            "data": data,
            **kwargs
        }
        
        self._write_jsonl(self._get_daily_log_path("summary"), log_entry)
        self.logger.info(f"Summary logged to file: {summary_type}")
    
    def _extract_latest_prices(self, market_data: Dict[str, Any]) -> Dict[str, float]:
        """Extract the latest price for each symbol from market data."""
        latest_prices = {}
        historical_ticks = market_data.get("historical_ticks", {})
        
        # Handle case where historical_ticks might be a string or other type
        if not isinstance(historical_ticks, dict):
            return latest_prices
        
        for symbol, ticks in historical_ticks.items():
            if isinstance(ticks, list) and ticks:
                # Get the latest tick (highest timestamp) - handle both 'ts' and 'timestamp' fields
                latest_tick = max(ticks, key=lambda t: t.get("ts", 0) or t.get("timestamp", 0) if isinstance(t, dict) else 0)
                if isinstance(latest_tick, dict):
                    latest_prices[symbol] = latest_tick.get("price", 0.0)
        
        return latest_prices
    
    def _calculate_data_timespan(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate the timespan of the market data."""
        historical_ticks = market_data.get("historical_ticks", {})
        
        # Handle case where historical_ticks might be a string or other type
        if not isinstance(historical_ticks, dict):
            return {"earliest": None, "latest": None, "span_seconds": 0, "tick_count": 0}
        
        all_timestamps = []
        for ticks in historical_ticks.values():
            if isinstance(ticks, list):
                # Handle both 'ts' and 'timestamp' fields
                timestamps = [t.get("ts", 0) or t.get("timestamp", 0) for t in ticks if isinstance(t, dict)]
                all_timestamps.extend([ts for ts in timestamps if ts > 0])
        
        if not all_timestamps:
            return {"earliest": None, "latest": None, "span_seconds": 0, "tick_count": 0}
        
        earliest = min(all_timestamps)
        latest = max(all_timestamps)
        
        return {
            "earliest": earliest,
            "latest": latest, 
            "span_seconds": latest - earliest,
            "tick_count": len(all_timestamps)
        }
    
    def _write_jsonl(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Write a JSON line to the specified file."""
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, separators=(',', ':')) + "\n")
        except Exception as e:
            self.logger.error(f"Failed to write to {file_path}: {e}")
    
    async def get_recent_decisions(self, limit: int = 10, days: int = 7) -> list[Dict[str, Any]]:
        """Get recent decisions from the workflow or fallback to log files."""
        if self.temporal_client:
            try:
                workflow_id = self._get_workflow_id()
                handle = self.temporal_client.get_workflow_handle(workflow_id)
                return await handle.query("get_recent_decisions", limit)
            except Exception as exc:
                self.logger.error(f"Failed to get decisions from workflow: {exc}")
                # Fallback to file-based
                return self._read_recent_from_daily_logs("decisions", limit, days)
        else:
            return self._read_recent_from_daily_logs("decisions", limit, days)
    
    async def get_recent_actions(self, limit: int = 10, days: int = 7) -> list[Dict[str, Any]]:
        """Get recent actions from the workflow or fallback to log files.""" 
        if self.temporal_client:
            try:
                workflow_id = self._get_workflow_id()
                handle = self.temporal_client.get_workflow_handle(workflow_id)
                return await handle.query("get_recent_actions", limit)
            except Exception as exc:
                self.logger.error(f"Failed to get actions from workflow: {exc}")
                # Fallback to file-based
                return self._read_recent_from_daily_logs("actions", limit, days)
        else:
            return self._read_recent_from_daily_logs("actions", limit, days)
    
    async def get_recent_summaries(self, limit: int = 10, days: int = 7) -> list[Dict[str, Any]]:
        """Get recent summaries from the workflow or fallback to log files."""
        if self.temporal_client:
            try:
                workflow_id = self._get_workflow_id()
                handle = self.temporal_client.get_workflow_handle(workflow_id)
                return await handle.query("get_logs", {"event_type": "summary", "limit": limit})
            except Exception as exc:
                self.logger.error(f"Failed to get summaries from workflow: {exc}")
                # Fallback to file-based
                return self._read_recent_from_daily_logs("summary", limit, days)
        else:
            return self._read_recent_from_daily_logs("summary", limit, days)
    
    def _read_recent_from_daily_logs(self, log_type: str, limit: int, days: int) -> list[Dict[str, Any]]:
        """Read recent entries from multiple daily log files."""
        all_entries = []
        
        # Get files for the last 'days' days
        for i in range(days):
            date = datetime.now(timezone.utc) - timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            file_path = self.log_dir / f"{self.agent_name}_{log_type}_{date_str}.jsonl"
            
            if file_path.exists():
                entries = self._read_recent_jsonl(file_path, limit * 2)  # Read more to ensure we get enough
                all_entries.extend(entries)
        
        # Sort by timestamp (descending) and return most recent
        all_entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return all_entries[:limit]
    
    def _read_recent_jsonl(self, file_path: Path, limit: int) -> list[Dict[str, Any]]:
        """Read the most recent entries from a JSONL file."""
        if not file_path.exists():
            return []
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            # Get the last 'limit' lines
            recent_lines = lines[-limit:] if len(lines) > limit else lines
            
            # Parse JSON lines
            entries = []
            for line in recent_lines:
                try:
                    entries.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
            
            return entries
            
        except Exception as e:
            self.logger.error(f"Failed to read from {file_path}: {e}")
            return []