"""LLM as Judge agent for evaluating and improving execution agent performance."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta

import openai
from temporalio.client import Client, RPCError, RPCStatusCode
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agents.prompt_manager import create_prompt_manager
from agents.workflows import JudgeAgentWorkflow, ExecutionLedgerWorkflow
from agents.workflows.agent_logging_workflow import AgentLoggingWorkflow
from tools.performance_analysis import PerformanceAnalyzer, format_performance_report
from tools.agent_logger import AgentLogger

ORANGE = "\033[33m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class JudgeAgent:
    """LLM as Judge agent for performance evaluation and prompt optimization."""
    
    def __init__(self, temporal_client: Client, mcp_session: ClientSession):
        self.temporal_client = temporal_client
        self.mcp_session = mcp_session
        self.performance_analyzer = PerformanceAnalyzer()
        self.prompt_manager = None
        self.agent_logger = AgentLogger("judge_agent", temporal_client)
        self.user_preferences = {}  # Cache user preferences
        
        # Evaluation criteria weights
        self.criteria_weights = {
            "returns": 0.30,
            "risk_management": 0.25,
            "decision_quality": 0.25,
            "consistency": 0.20
        }
        
        # Performance thresholds for prompt updates
        self.update_thresholds = {
            "min_evaluation_score": 60.0,  # Only update if score is above this
            "max_drawdown_trigger": 0.20,  # Trigger conservative mode
            "poor_performance_trigger": 40.0,  # Score below this triggers intervention
            "improvement_threshold": 5.0,  # Minimum improvement to justify update
        }
    
    async def initialize(self) -> None:
        """Initialize the judge agent."""
        self.prompt_manager = await create_prompt_manager(self.temporal_client)
        
        # Ensure agent logging workflow exists
        try:
            handle = self.temporal_client.get_workflow_handle("agent-logging")
            await handle.describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                await self.temporal_client.start_workflow(
                    AgentLoggingWorkflow.run,
                    id="agent-logging",
                    task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
                )
                logger.info("Started agent logging workflow")
        
        # Ensure judge workflow exists
        try:
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            await handle.describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                await self.temporal_client.start_workflow(
                    JudgeAgentWorkflow.run,
                    id="judge-agent",
                    task_queue=os.environ.get("TASK_QUEUE", "mcp-tools"),
                )
                logger.info("Started judge agent workflow")
    
    async def should_evaluate(self) -> bool:
        """Check if it's time for a new evaluation."""
        try:
            # First check for immediate evaluation triggers
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            recent_evaluations = await handle.query("get_evaluations", {"limit": 5, "since_ts": 0})  # Get last 5 evaluations
            
            # Check if there's a pending trigger request
            for evaluation in recent_evaluations:
                if (evaluation.get("type") == "immediate_trigger" and 
                    evaluation.get("status") == "trigger_requested"):
                    logger.info("Found immediate evaluation trigger, proceeding with evaluation")
                    return True
            
            # Check if ledger workflow exists and has data
            ledger_handle = self.temporal_client.get_workflow_handle(
                os.environ.get("LEDGER_WF_ID", "mock-ledger")
            )
            
            try:
                await ledger_handle.describe()
                # Check if there are any transactions to evaluate
                recent_transactions = await ledger_handle.query("get_transaction_history", {"since_ts": 0, "limit": 1})
                if not recent_transactions:
                    logger.info("No transactions found yet, skipping evaluation")
                    return False
            except Exception:
                logger.info("Ledger workflow not ready yet, skipping evaluation")
                return False
            
            # Check cooldown timing
            return await handle.query("should_trigger_evaluation", 0.167)  # 10 minute cooldown (0.167 hours)
        except Exception as exc:
            logger.debug("Failed to check evaluation timing: %s", exc)
            return False
    
    async def collect_performance_data(self, window_days: int = 7) -> Dict:
        """Collect comprehensive performance data for evaluation."""
        try:
            # Get performance metrics from ledger
            ledger_handle = self.temporal_client.get_workflow_handle(
                os.environ.get("LEDGER_WF_ID", "mock-ledger")
            )
            
            # Verify workflow exists and is running
            try:
                await ledger_handle.describe()
            except Exception as desc_exc:
                if "not found" in str(desc_exc).lower():
                    logger.info("Ledger workflow not found - no trading data available yet")
                    return {
                        "performance_metrics": {"total_trades": 0, "total_pnl": 0.0, "max_drawdown": 0.0, "win_rate": 0.0},
                        "risk_metrics": {"total_portfolio_value": 250000.0, "cash_ratio": 1.0, "max_position_concentration": 0.0, "num_positions": 0},
                        "transaction_history": [],
                        "evaluation_period_days": window_days,
                        "status": "no_data"
                    }
                else:
                    raise desc_exc
            
            performance_metrics = await ledger_handle.query("get_performance_metrics", window_days)
            risk_metrics = await ledger_handle.query("get_risk_metrics")
            transaction_history = await ledger_handle.query("get_transaction_history", {
                "since_ts": int((datetime.now(timezone.utc) - timedelta(days=window_days)).timestamp()),
                "limit": 500
            })
            
            return {
                "performance_metrics": performance_metrics,
                "risk_metrics": risk_metrics,
                "transaction_history": transaction_history,
                "evaluation_period_days": window_days,
                "status": "success"
            }
            
        except Exception as exc:
            logger.error("Failed to collect performance data: %s", exc)
            return {
                "performance_metrics": {"total_trades": 0, "total_pnl": 0.0, "max_drawdown": 0.0, "win_rate": 0.0},
                "risk_metrics": {"total_portfolio_value": 250000.0, "cash_ratio": 1.0, "max_position_concentration": 0.0, "num_positions": 0},
                "transaction_history": [],
                "evaluation_period_days": window_days,
                "status": "error",
                "error": str(exc)
            }
    
    async def analyze_decision_quality(self, transaction_history: List[Dict]) -> Dict:
        """Use LLM to analyze decision quality from transaction patterns."""
        if not transaction_history:
            return {
                "decision_score": 50.0,
                "reasoning": "No transactions to analyze",
                "recommendations": []
            }
        
        # Prepare transaction summary for LLM analysis
        recent_transactions = transaction_history[:20]  # Last 20 transactions
        transaction_summary = "\n".join([
            f"Time: {datetime.fromtimestamp(tx['timestamp']).strftime('%Y-%m-%d %H:%M')} | "
            f"{tx['side']} {tx['quantity']:.2f} {tx['symbol']} @ ${tx['fill_price']:.2f} | "
            f"Cost: ${tx['cost']:.2f}"
            for tx in recent_transactions
        ])
        
        # Calculate basic statistics
        symbols = list(set(tx['symbol'] for tx in transaction_history))
        buy_count = len([tx for tx in transaction_history if tx['side'] == 'BUY'])
        sell_count = len([tx for tx in transaction_history if tx['side'] == 'SELL'])
        
        analysis_prompt = f"""
Analyze the following cryptocurrency trading decisions for quality and effectiveness:

RECENT TRANSACTIONS:
{transaction_summary}

TRADING STATISTICS:
- Total transactions: {len(transaction_history)}
- Buy orders: {buy_count}
- Sell orders: {sell_count}
- Symbols traded: {', '.join(symbols)}

Please evaluate:
1. Decision timing and market awareness
2. Position sizing consistency
3. Risk management adherence
4. Portfolio diversification approach

Provide a score from 0-100 and specific recommendations for improvement.
Respond in JSON format:
{{
    "decision_score": <number>,
    "reasoning": "<detailed analysis>",
    "recommendations": ["<recommendation1>", "<recommendation2>", ...]
}}
"""
        
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert trading analyst evaluating algorithmic trading decisions. Provide objective, data-driven analysis."
                    },
                    {
                        "role": "user",
                        "content": analysis_prompt
                    }
                ],
                max_tokens=800,
                temperature=0.1
            )
            
            analysis_text = response.choices[0].message.content
            # Extract JSON from response
            start_idx = analysis_text.find('{')
            end_idx = analysis_text.rfind('}') + 1
            
            if start_idx >= 0 and end_idx > start_idx:
                return json.loads(analysis_text[start_idx:end_idx])
            else:
                raise ValueError("No valid JSON found in LLM response")
                
        except Exception as exc:
            logger.error("Failed to analyze decision quality: %s", exc)
            return {
                "decision_score": 50.0,
                "reasoning": f"Analysis failed: {exc}",
                "recommendations": ["Review decision analysis system"]
            }
    
    async def generate_evaluation_report(self, performance_data: Dict) -> Dict:
        """Generate comprehensive evaluation report."""
        performance_metrics = performance_data.get("performance_metrics", {})
        risk_metrics = performance_data.get("risk_metrics", {})
        transaction_history = performance_data.get("transaction_history", [])
        
        # Generate detailed performance report
        performance_report = self.performance_analyzer.generate_performance_report(
            transactions=transaction_history,
            performance_metrics=performance_metrics,
            risk_metrics=risk_metrics
        )
        
        # Analyze decision quality using LLM
        decision_analysis = await self.analyze_decision_quality(transaction_history)
        
        # Calculate overall evaluation score
        return_score = min(100.0, max(0.0, (performance_report.annualized_return + 1) * 50))
        risk_score = performance_report.risk_management_score
        decision_score = decision_analysis["decision_score"]
        consistency_score = performance_report.consistency_score
        
        overall_score = (
            return_score * self.criteria_weights["returns"] +
            risk_score * self.criteria_weights["risk_management"] +
            decision_score * self.criteria_weights["decision_quality"] +
            consistency_score * self.criteria_weights["consistency"]
        )
        
        return {
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "evaluation_period_days": performance_data.get("evaluation_period_days", 7),
            "overall_score": overall_score,
            "component_scores": {
                "returns": return_score,
                "risk_management": risk_score,
                "decision_quality": decision_score,
                "consistency": consistency_score
            },
            "performance_report": performance_report,
            "decision_analysis": decision_analysis,
            "metrics": {
                "total_trades": len(transaction_history),
                "annualized_return": performance_report.annualized_return,
                "sharpe_ratio": performance_report.sharpe_ratio,
                "max_drawdown": performance_report.max_drawdown,
                "win_rate": performance_report.win_rate
            },
            "recommendations": decision_analysis.get("recommendations", [])
        }
    
    async def get_user_preferences(self) -> Dict:
        """Retrieve user preferences from judge workflow."""
        try:
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            prefs = await handle.query("get_user_preferences")
            self.user_preferences = prefs
            return prefs
        except Exception as exc:
            logger.warning("Failed to get user preferences from judge workflow: %s", exc)
            return {}
    
    def parse_percentage(self, value: str, default: float) -> float:
        """Parse percentage string to float (e.g., '50%' -> 0.5)."""
        try:
            if isinstance(value, str) and value.endswith('%'):
                return float(value.rstrip('%')) / 100.0
            elif isinstance(value, (int, float)):
                return float(value) if value <= 1.0 else float(value) / 100.0
            else:
                return default
        except (ValueError, TypeError):
            return default
    
    def get_user_baseline_limits(self) -> Dict[str, float]:
        """Get user's baseline risk limits from preferences."""
        prefs = self.user_preferences
        
        # Parse user preferences with fallback defaults
        baseline = {
            "max_position_size": self.parse_percentage(prefs.get('position_size_comfort', '20%'), 0.20),
            "cash_reserve": self.parse_percentage(prefs.get('cash_reserve_level', '15%'), 0.15),
            "drawdown_tolerance": self.parse_percentage(prefs.get('drawdown_tolerance', '15%'), 0.15),
            "risk_tolerance": prefs.get('risk_tolerance', 'moderate')
        }
        
        logger.info("User baseline limits: position=%.1f%%, cash=%.1f%%, drawdown=%.1f%%, risk=%s",
                   baseline["max_position_size"] * 100,
                   baseline["cash_reserve"] * 100, 
                   baseline["drawdown_tolerance"] * 100,
                   baseline["risk_tolerance"])
        
        return baseline
    
    async def determine_context_updates(self, evaluation: Dict) -> Optional[Dict]:
        """Determine if context updates are needed based on evaluation."""
        overall_score = evaluation["overall_score"]
        performance_report = evaluation["performance_report"]
        component_scores = evaluation["component_scores"]
        
        # Get user preferences and baseline limits
        await self.get_user_preferences()
        baseline = self.get_user_baseline_limits()
        
        # Check if performance is poor enough to warrant intervention
        if overall_score < self.update_thresholds["poor_performance_trigger"]:
            # Emergency intervention - reduce risk from user's baseline
            conservative_position = baseline["max_position_size"] * 0.5  # 50% of user's comfort level
            conservative_cash = min(baseline["cash_reserve"] * 1.5, 0.30)  # 1.5x user's preference, capped at 30%
            
            return {
                "update_type": "emergency_conservative",
                "reason": f"Poor performance (score: {overall_score:.1f}) requires immediate risk reduction",
                "context": {
                    "risk_mode": "conservative",
                    "performance_trend": ["declining", "poor"]
                },
                "changes": [
                    "Switched to conservative risk mode",
                    f"Reduced maximum position size to {conservative_position:.0%} (50% of your {baseline['max_position_size']:.0%} comfort level)",
                    f"Increased cash reserves to {conservative_cash:.0%} (from your {baseline['cash_reserve']:.0%} preference)",
                    "Enhanced risk controls for trade sizing"
                ]
            }
        
        # Check for high drawdown using user's tolerance as baseline
        drawdown_trigger = baseline["drawdown_tolerance"] * 1.5  # Trigger when 1.5x user's tolerance
        if performance_report.max_drawdown > drawdown_trigger:
            conservative_position = baseline["max_position_size"] * 0.7  # 70% of user's comfort level
            conservative_cash = min(baseline["cash_reserve"] * 1.3, 0.25)  # 1.3x user's preference
            
            return {
                "update_type": "risk_reduction",
                "reason": f"High drawdown ({performance_report.max_drawdown:.1%}) exceeds {drawdown_trigger:.1%} trigger (1.5x your {baseline['drawdown_tolerance']:.1%} tolerance)",
                "context": {
                    "risk_mode": "conservative",
                    "performance_trend": ["declining"]
                },
                "changes": [
                    "Switched to conservative risk mode",
                    "Enhanced drawdown protection",
                    f"Reduced position sizing to {conservative_position:.0%} (70% of your {baseline['max_position_size']:.0%} comfort level)",
                    f"Increased cash reserves to {conservative_cash:.0%}"
                ]
            }
        
        # Check decision quality issues
        if component_scores["decision_quality"] < 40:
            return {
                "update_type": "decision_improvement",
                "reason": "Poor decision quality requires enhanced decision framework",
                "context": {
                    "risk_mode": "moderate",
                    "performance_trend": ["declining", "poor"]
                },
                "changes": [
                    "Added performance monitoring guidelines",
                    "Enhanced decision analysis requirements",
                    "Improved market analysis framework"
                ]
            }
        
        # Check for overly conservative performance (relative to user's risk tolerance)
        conservative_drawdown_threshold = baseline["drawdown_tolerance"] * 0.3  # Much less than user's tolerance
        low_return_threshold = 0.02  # Still use 2% as low return threshold
        
        if (performance_report.max_drawdown < conservative_drawdown_threshold and 
            performance_report.total_return < low_return_threshold and 
            overall_score > 60):
            
            # Only increase if user has higher risk tolerance
            if baseline["risk_tolerance"] in ["high", "aggressive"]:
                aggressive_position = min(baseline["max_position_size"] * 1.2, 0.80)  # Up to 120% of user comfort, capped at 80%
                aggressive_cash = max(baseline["cash_reserve"] * 0.8, 0.05)  # 80% of user preference, minimum 5%
                
                return {
                    "update_type": "increase_aggressiveness",
                    "reason": f"Very low drawdown ({performance_report.max_drawdown:.1%} vs your {baseline['drawdown_tolerance']:.1%} tolerance) suggests room for more risk",
                    "context": {
                        "risk_mode": "aggressive",
                        "performance_trend": ["stable"]
                    },
                    "changes": [
                        "Switched to aggressive risk mode",
                        f"Increased position sizing to {aggressive_position:.0%} (up to 120% of your {baseline['max_position_size']:.0%} comfort level)",
                        f"Reduced cash reserves to {aggressive_cash:.0%} (80% of your {baseline['cash_reserve']:.0%} preference)",
                        "More aggressive risk parameters aligned with your risk tolerance"
                    ]
                }
            else:
                # User prefers conservative/moderate - don't increase aggressiveness
                return None
        
        # No updates needed
        return None
    
    async def implement_context_update(self, update_spec: Dict) -> bool:
        """Implement the specified context update."""
        try:
            update_type = update_spec["update_type"]
            new_context = update_spec["context"]
            reason = update_spec["reason"]
            changes = update_spec["changes"]
            
            # Update the context
            success = await self.prompt_manager.update_agent_context(
                agent_type="execution_agent",
                new_context=new_context,
                description=f"{update_type.replace('_', ' ').title()} update",
                reason=reason,
                changes=changes
            )
            
            if success:
                print(f"{GREEN}[JudgeAgent] Implemented context update: {update_type}{RESET}")
                print(f"{CYAN}[JudgeAgent] Reason: {reason}{RESET}")
                print(f"{CYAN}[JudgeAgent] New context: {new_context}{RESET}")
                for change in changes:
                    print(f"{CYAN}[JudgeAgent] - {change}{RESET}")
                
                # Display the rendered prompt with new context
                new_prompt = await self.prompt_manager.get_current_prompt(
                    agent_type="execution_agent",
                    context=new_context
                )
                
                print(f"{GREEN}[JudgeAgent] New Execution Agent Prompt:{RESET}")
                print(f"{CYAN}{'='*80}{RESET}")
                
                prompt_lines = new_prompt.split('\n')
                for line in prompt_lines:
                    print(f"{CYAN}{line}{RESET}")
                
                print(f"{CYAN}{'='*80}{RESET}")
                print(f"{GREEN}[JudgeAgent] Context update complete (length: {len(new_prompt)} chars, {len(prompt_lines)} lines){RESET}")
            
            return success
            
        except Exception as exc:
            logger.error("Failed to implement context update: %s", exc)
            return False
    
    async def record_evaluation(self, evaluation: Dict) -> None:
        """Record the evaluation in the judge workflow."""
        try:
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            await handle.signal("record_evaluation", evaluation)
            logger.info("Recorded evaluation with score %.1f", evaluation["overall_score"])
        except Exception as exc:
            logger.error("Failed to record evaluation: %s", exc)
    
    async def _mark_triggers_processed(self) -> None:
        """Mark any pending trigger requests as processed."""
        try:
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            await handle.signal("mark_triggers_processed", {})
        except Exception as exc:
            logger.debug("Failed to mark triggers as processed: %s", exc)
    
    async def run_evaluation_cycle(self) -> None:
        """Run a complete evaluation cycle."""
        print(f"{ORANGE}[JudgeAgent] Starting evaluation cycle{RESET}")
        
        try:
            # Collect performance data
            print(f"{CYAN}[JudgeAgent] Collecting performance data...{RESET}")
            performance_data = await self.collect_performance_data(window_days=7)
            
            if not performance_data:
                print(f"{RED}[JudgeAgent] Failed to collect performance data{RESET}")
                return
            
            # Handle the case where there's no trading data yet
            status = performance_data.get("status", "unknown")
            if status == "no_data":
                print(f"{CYAN}[JudgeAgent] No trading data available yet - skipping evaluation{RESET}")
                print(f"{CYAN}[JudgeAgent] Waiting for execution agent to start trading...{RESET}")
                return
            elif status == "error":
                print(f"{RED}[JudgeAgent] Error collecting data: {performance_data.get('error', 'Unknown error')}{RESET}")
                return
            
            # Generate evaluation report
            print(f"{CYAN}[JudgeAgent] Generating evaluation report...{RESET}")
            evaluation = await self.generate_evaluation_report(performance_data)
            
            # Display results
            print(f"{GREEN}[JudgeAgent] Evaluation completed{RESET}")
            print(f"{CYAN}[JudgeAgent] Overall Score: {evaluation['overall_score']:.1f}/100{RESET}")
            print(f"{CYAN}[JudgeAgent] Component Scores:{RESET}")
            for component, score in evaluation["component_scores"].items():
                print(f"{CYAN}[JudgeAgent]   {component}: {score:.1f}{RESET}")
            
            # Check for context updates
            update_spec = await self.determine_context_updates(evaluation)
            if update_spec:
                print(f"{ORANGE}[JudgeAgent] Context update recommended: {update_spec['update_type']}{RESET}")
                success = await self.implement_context_update(update_spec)
                evaluation["context_update"] = {
                    "implemented": success,
                    "update_spec": update_spec
                }
            else:
                print(f"{GREEN}[JudgeAgent] No context updates needed{RESET}")
                evaluation["context_update"] = {"implemented": False}
            
            # Record evaluation
            await self.record_evaluation(evaluation)
            
            # Log comprehensive evaluation to workflow
            try:
                await self.agent_logger.log_summary(
                    summary_type="performance_evaluation",
                    data={
                        "evaluation_period_days": evaluation.get("evaluation_period_days", 7),
                        "overall_score": evaluation["overall_score"],
                        "component_scores": evaluation["component_scores"],
                        "performance_metrics": {
                            "total_trades": evaluation["metrics"]["total_trades"],
                            "annualized_return": evaluation["metrics"]["annualized_return"],
                            "sharpe_ratio": evaluation["metrics"]["sharpe_ratio"],
                            "max_drawdown": evaluation["metrics"]["max_drawdown"],
                            "win_rate": evaluation["metrics"]["win_rate"]
                        },
                        "decision_analysis": evaluation["decision_analysis"],
                        "context_update": evaluation.get("context_update", {"implemented": False}),
                        "recommendations": evaluation.get("recommendations", [])
                    },
                    performance_data=performance_data,
                    trigger_type=performance_data.get("trigger_type", "scheduled")
                )
                
                # Log context update action if one was implemented
                if evaluation.get("context_update", {}).get("implemented"):
                    await self.agent_logger.log_action(
                        action_type="context_update",
                        details=evaluation["context_update"]["update_spec"],
                        result={"success": True}
                    )
                    
            except Exception as log_error:
                logger.error(f"Failed to log evaluation: {log_error}")
            
            # Mark any trigger requests as processed
            await self._mark_triggers_processed()
            
            # Display detailed report if requested
            if os.environ.get("JUDGE_VERBOSE", "false").lower() == "true":
                report_text = format_performance_report(evaluation["performance_report"])
                print(f"{CYAN}[JudgeAgent] Detailed Report:{RESET}")
                print(report_text)
            
        except Exception as exc:
            logger.error("Evaluation cycle failed: %s", exc)
            print(f"{RED}[JudgeAgent] Evaluation cycle failed: {exc}{RESET}")


async def _watch_judge_preferences(client: Client, current_preferences: dict) -> None:
    """Poll judge agent workflow for user preference updates."""
    wf_id = "judge-agent"
    while True:
        try:
            handle = client.get_workflow_handle(wf_id)
            prefs = await handle.query("get_user_preferences")
            
            # Check if preferences have changed
            if prefs != current_preferences:
                current_preferences.clear()
                current_preferences.update(prefs)
                
                if prefs:
                    print(f"{GREEN}[JudgeAgent] ✅ User preferences updated: risk_tolerance={prefs.get('risk_tolerance', 'unknown')}, position_comfort={prefs.get('position_size_comfort', 'unknown')}, cash_reserve={prefs.get('cash_reserve_level', 'unknown')}{RESET}")
                    
                    # Show calculated baseline limits
                    try:
                        # Parse baseline limits like the judge agent does
                        max_position = float(prefs.get('position_size_comfort', '20%').rstrip('%')) / 100.0 if isinstance(prefs.get('position_size_comfort'), str) else prefs.get('position_size_comfort', 0.20)
                        cash_reserve = float(prefs.get('cash_reserve_level', '15%').rstrip('%')) / 100.0 if isinstance(prefs.get('cash_reserve_level'), str) else prefs.get('cash_reserve_level', 0.15)
                        drawdown_tolerance = float(prefs.get('drawdown_tolerance', '15%').rstrip('%')) / 100.0 if isinstance(prefs.get('drawdown_tolerance'), str) else prefs.get('drawdown_tolerance', 0.15)
                        
                        print(f"{GREEN}[JudgeAgent] Baseline limits: position={max_position:.0%}, cash={cash_reserve:.0%}, drawdown_tolerance={drawdown_tolerance:.0%}{RESET}")
                    except Exception as e:
                        logger.warning("Failed to parse baseline limits: %s", e)
                else:
                    print(f"{CYAN}[JudgeAgent] No user preferences set - using defaults{RESET}")
                    
        except Exception as exc:
            # Silently continue if judge agent workflow not found or other issues
            pass
        
        await asyncio.sleep(3)  # Check every 3 seconds


async def run_judge_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the judge agent with periodic evaluations."""
    mcp_url = server_url.rstrip("/") + "/mcp/"
    
    # Connect to Temporal
    temporal = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    
    # Connect to MCP server
    async with streamablehttp_client(mcp_url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            
            # Initialize judge agent
            judge = JudgeAgent(temporal, session)
            await judge.initialize()
            
            print(f"{GREEN}[JudgeAgent] Judge agent started{RESET}")
            
            # Start watching for user preference updates (similar to execution agent)
            current_preferences = {}
            _preferences_task = asyncio.create_task(_watch_judge_preferences(temporal, current_preferences))
            
            print(f"{CYAN}[JudgeAgent] Waiting for trading activity before starting evaluations...{RESET}")
            
            # Main evaluation loop
            startup_delay = True
            while True:
                try:
                    # On startup, wait longer to let system stabilize
                    if startup_delay:
                        print(f"{CYAN}[JudgeAgent] Initial startup delay - waiting 5 minutes for system to stabilize{RESET}")
                        await asyncio.sleep(5 * 60)  # 5 minute initial delay
                        startup_delay = False
                    
                    # Check if evaluation is needed
                    if await judge.should_evaluate():
                        await judge.run_evaluation_cycle()
                    else:
                        # Log why we're not evaluating (but less frequently)
                        print(f"{CYAN}[JudgeAgent] Evaluation not needed - checking again in 5 minutes{RESET}")
                    
                    # Sleep for 5 minutes before checking again
                    await asyncio.sleep(5 * 60)
                    
                except KeyboardInterrupt:
                    print(f"{ORANGE}[JudgeAgent] Shutting down...{RESET}")
                    break
                except Exception as exc:
                    logger.error("Judge agent error: %s", exc)
                    # Sleep longer on error to avoid spam
                    await asyncio.sleep(5 * 60)


if __name__ == "__main__":
    asyncio.run(run_judge_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))