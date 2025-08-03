"""Execution ledger workflow for maintaining mock execution state."""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List
from datetime import datetime, timezone
from temporalio import workflow


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