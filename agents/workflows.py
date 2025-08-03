"""Temporal workflow implementations backing the agents."""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List
from datetime import datetime, timezone

from temporalio import workflow


@workflow.defn
class EnsembleWorkflow:
    """Store latest prices and approved intents."""

    def __init__(self) -> None:
        self.latest_price: Dict[str, float] = {}
        self.intents: List[Dict] = []

    @workflow.signal
    def update_price(self, symbol: str, price: float) -> None:
        self.latest_price[symbol] = price

    @workflow.signal
    def record_intent(self, intent: Dict) -> None:
        self.intents.append(intent)

    @workflow.query
    def get_price(self, symbol: str) -> float | None:
        return self.latest_price.get(symbol)

    @workflow.query
    def get_intents(self) -> List[Dict]:
        return list(self.intents)

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)




@workflow.defn
class ExecutionLedgerWorkflow:
    """Maintain mock execution ledger state."""

    def __init__(self) -> None:
        self.initial_cash = Decimal("250000")
        self.cash = Decimal("250000")
        self.positions: Dict[str, Decimal] = {}
        self.last_price: Dict[str, Decimal] = {}
        self.entry_price: Dict[str, Decimal] = {}
        self.fill_count = 0
        self.transaction_history: List[Dict] = []

    @workflow.signal
    def record_fill(self, fill: Dict) -> None:
        side = fill["side"]
        symbol = fill["symbol"]
        qty = Decimal(str(fill["qty"]))
        price = Decimal(str(fill["fill_price"]))
        cost = Decimal(str(fill["cost"]))
        
        # Add transaction to history with timestamp
        transaction = {
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "side": side,
            "symbol": symbol,
            "quantity": float(qty),
            "fill_price": float(price),
            "cost": float(cost),
            "cash_before": float(self.cash),
            "position_before": float(self.positions.get(symbol, Decimal("0")))
        }
        self.transaction_history.append(transaction)
        
        self.last_price[symbol] = price
        current_qty = self.positions.get(symbol, Decimal("0"))
        if side == "BUY":
            self.cash -= cost
            new_qty = current_qty + qty
            avg_price = self.entry_price.get(symbol, Decimal("0"))
            if new_qty > 0:
                self.entry_price[symbol] = (
                    (avg_price * current_qty + price * qty) / new_qty
                )
            self.positions[symbol] = new_qty
        else:
            self.cash += cost
            new_qty = current_qty - qty
            if new_qty <= 0:
                self.positions.pop(symbol, None)
                self.entry_price.pop(symbol, None)
            else:
                self.positions[symbol] = new_qty
        self.fill_count += 1

    @workflow.query
    def get_pnl(self) -> float:
        position_value = sum(
            q * self.last_price.get(sym, Decimal("0"))
            for sym, q in self.positions.items()
        )
        pnl = (self.cash + position_value) - self.initial_cash
        return float(pnl)

    @workflow.query
    def get_cash(self) -> float:
        return float(self.cash)

    @workflow.query
    def get_positions(self) -> Dict[str, float]:
        """Return current position sizes as floats."""
        return {sym: float(q) for sym, q in self.positions.items()}

    @workflow.query
    def get_entry_prices(self) -> Dict[str, float]:
        """Return weighted average entry price for each symbol."""
        return {sym: float(p) for sym, p in self.entry_price.items()}

    @workflow.query
    def get_transaction_history(self, params: Dict = None) -> List[Dict]:
        """Return transaction history filtered by timestamp and limited by count."""
        if params is None:
            params = {}
        
        since_ts = params.get("since_ts", 0)
        limit = params.get("limit", 1000)
        
        filtered_transactions = [
            tx for tx in self.transaction_history 
            if tx["timestamp"] >= since_ts
        ]
        # Return most recent transactions first
        filtered_transactions.sort(key=lambda x: x["timestamp"], reverse=True)
        return filtered_transactions[:limit]

    @workflow.query
    def get_performance_metrics(self, window_days: int = 30) -> Dict[str, float]:
        """Calculate performance metrics for the specified time window."""
        current_time = int(datetime.now(timezone.utc).timestamp())
        window_start = current_time - (window_days * 24 * 60 * 60)
        
        # Filter transactions within the window
        window_transactions = [
            tx for tx in self.transaction_history 
            if tx["timestamp"] >= window_start
        ]
        
        if not window_transactions:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_trade_pnl": 0.0,
                "total_pnl": float(self.get_pnl()),
                "max_drawdown": 0.0,
                "sharpe_ratio": 0.0
            }
        
        # Calculate basic metrics
        total_trades = len(window_transactions)
        profitable_trades = 0
        total_trade_pnl = 0.0
        portfolio_values = []
        
        # Track portfolio value over time for drawdown calculation
        running_cash = float(self.initial_cash)
        running_positions = {}
        
        for tx in sorted(window_transactions, key=lambda x: x["timestamp"]):
            symbol = tx["symbol"]
            side = tx["side"]
            qty = tx["quantity"]
            cost = tx["cost"]
            
            if side == "BUY":
                running_cash -= cost
                running_positions[symbol] = running_positions.get(symbol, 0) + qty
            else:
                running_cash += cost
                running_positions[symbol] = running_positions.get(symbol, 0) - qty
                if running_positions[symbol] <= 0:
                    running_positions.pop(symbol, None)
            
            # Calculate portfolio value (simplified - using current prices)
            position_value = sum(
                pos * float(self.last_price.get(sym, Decimal("0")))
                for sym, pos in running_positions.items()
            )
            portfolio_value = running_cash + position_value
            portfolio_values.append(portfolio_value)
        
        # Calculate win rate and average PnL (simplified)
        current_portfolio_value = float(self.initial_cash) + float(self.get_pnl())
        total_pnl = current_portfolio_value - float(self.initial_cash)
        
        # Calculate max drawdown
        max_drawdown = 0.0
        if portfolio_values:
            peak = portfolio_values[0]
            for value in portfolio_values:
                if value > peak:
                    peak = value
                drawdown = (peak - value) / peak if peak > 0 else 0
                max_drawdown = max(max_drawdown, drawdown)
        
        # Simplified metrics calculation
        avg_trade_pnl = total_pnl / total_trades if total_trades > 0 else 0.0
        win_rate = 0.5  # Placeholder - would need more sophisticated calculation
        sharpe_ratio = 0.0  # Placeholder - would need returns time series
        
        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_trade_pnl": avg_trade_pnl,
            "total_pnl": total_pnl,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio
        }

    @workflow.query
    def get_risk_metrics(self) -> Dict[str, float]:
        """Calculate current risk metrics."""
        total_portfolio_value = float(self.cash)
        
        # Add position values
        for symbol, qty in self.positions.items():
            price = float(self.last_price.get(symbol, Decimal("0")))
            total_portfolio_value += float(qty) * price
        
        # Calculate position concentration
        position_concentrations = {}
        for symbol, qty in self.positions.items():
            price = float(self.last_price.get(symbol, Decimal("0")))
            position_value = float(qty) * price
            concentration = position_value / total_portfolio_value if total_portfolio_value > 0 else 0
            position_concentrations[symbol] = concentration
        
        max_position_concentration = max(position_concentrations.values()) if position_concentrations else 0.0
        cash_ratio = float(self.cash) / total_portfolio_value if total_portfolio_value > 0 else 1.0
        
        return {
            "total_portfolio_value": total_portfolio_value,
            "cash_ratio": cash_ratio,
            "max_position_concentration": max_position_concentration,
            "num_positions": len(self.positions),
            "leverage": 1.0  # Assuming no leverage for now
        }

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


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
