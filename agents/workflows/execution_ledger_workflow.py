"""Execution ledger workflow for maintaining mock execution state."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Dict, List, Any
from datetime import datetime, timezone
from temporalio import workflow
from temporalio.client import Client


@workflow.defn
class ExecutionLedgerWorkflow:
    """Maintain mock execution ledger state."""

    def __init__(self) -> None:
        # Get initial balance from environment variable, default to 1000
        initial_balance = os.environ.get("INITIAL_PORTFOLIO_BALANCE", "1000")
        self.initial_cash = Decimal(initial_balance)
        self.cash = Decimal(initial_balance)
        self.positions: Dict[str, Decimal] = {}
        self.last_price: Dict[str, Decimal] = {}
        self.last_price_timestamp: Dict[str, int] = {}  # Track when prices were last updated
        self.entry_price: Dict[str, Decimal] = {}
        self.fill_count = 0
        self.transaction_history: List[Dict] = []
        self.realized_pnl = Decimal("0")  # Track actual realized gains/losses from closed positions
        self.price_staleness_threshold = 300  # 5 minutes in seconds
        self.profit_scraping_percentage = Decimal("0.20")  # Default 20% profit scraping
        self.scraped_profits = Decimal("0")  # Total profits set aside
        self.user_preferences: Dict[str, Any] = {}  # Store user preferences

    @workflow.signal
    def set_user_preferences(self, preferences: Dict[str, Any]) -> None:
        """Update user preferences including profit scraping percentage."""
        self.user_preferences = preferences
        # Parse profit scraping percentage
        profit_scraping = preferences.get('profit_scraping_percentage', '20%')
        if isinstance(profit_scraping, str) and profit_scraping.endswith('%'):
            self.profit_scraping_percentage = Decimal(profit_scraping.rstrip('%')) / 100
        elif profit_scraping:
            self.profit_scraping_percentage = Decimal(str(profit_scraping))
        else:
            self.profit_scraping_percentage = Decimal("0.20")  # Default 20%
        
        workflow.logger.info(f"Profit scraping set to {self.profit_scraping_percentage * 100}%")

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
        
        # Update price and timestamp
        current_timestamp = int(datetime.now(timezone.utc).timestamp())
        self.last_price[symbol] = price
        self.last_price_timestamp[symbol] = current_timestamp
        current_qty = self.positions.get(symbol, Decimal("0"))
        if side == "BUY":
            self.cash -= cost
            new_qty = current_qty + qty
            
            # Calculate weighted average entry price
            if current_qty == 0:
                # First purchase - entry price is the fill price
                self.entry_price[symbol] = price
            else:
                # Subsequent purchases - weighted average
                avg_price = self.entry_price.get(symbol, price)
                self.entry_price[symbol] = (
                    (avg_price * current_qty + price * qty) / new_qty
                )
            
            self.positions[symbol] = new_qty
        else:  # SELL
            self.cash += cost
            new_qty = current_qty - qty
            
            # Calculate realized PnL for the sold quantity
            if current_qty > 0 and symbol in self.entry_price:
                entry_price = self.entry_price[symbol]
                # Realized PnL = (sell_price - entry_price) * quantity_sold
                position_realized_pnl = (price - entry_price) * qty
                self.realized_pnl += position_realized_pnl
                
                # Apply profit scraping if this was a profitable trade
                if position_realized_pnl > 0:
                    scraped_amount = position_realized_pnl * self.profit_scraping_percentage
                    self.scraped_profits += scraped_amount
                    # Remove scraped profits from available cash
                    self.cash -= scraped_amount
                    workflow.logger.info(f"Scraped {scraped_amount:.2f} ({self.profit_scraping_percentage * 100}%) from {position_realized_pnl:.2f} profit")
            
            if new_qty <= 0:
                self.positions.pop(symbol, None)
                self.entry_price.pop(symbol, None)
            else:
                self.positions[symbol] = new_qty
        self.fill_count += 1
    
    def _validate_price(self, price: Decimal, symbol: str) -> bool:
        """Validate that a price is reasonable."""
        # Basic sanity checks
        if price <= 0:
            return False
        
        # Price should be reasonable (not astronomical)
        if price > Decimal("10000000"):  # $10M per unit seems unreasonable
            return False
        
        # Check for extreme price movements vs last known price
        if symbol in self.last_price:
            last_price = self.last_price[symbol]
            if last_price > 0:
                price_change_ratio = abs(price - last_price) / last_price
                # Reject prices that moved more than 90% in either direction
                # This catches obvious data errors while allowing for crypto volatility
                if price_change_ratio > Decimal("0.9"):
                    return False
        
        return True
    
    def _is_price_stale(self, symbol: str, max_age_seconds: int = None) -> bool:
        """Check if the last price for a symbol is stale."""
        if symbol not in self.last_price_timestamp:
            return True
        
        threshold = max_age_seconds or self.price_staleness_threshold
        current_time = int(datetime.now(timezone.utc).timestamp())
        price_age = current_time - self.last_price_timestamp[symbol]
        
        return price_age > threshold
    
    def _get_price_age(self, symbol: str) -> int:
        """Get the age of the last price in seconds."""
        if symbol not in self.last_price_timestamp:
            return float('inf')
        
        current_time = int(datetime.now(timezone.utc).timestamp())
        return current_time - self.last_price_timestamp[symbol]

    @workflow.query
    def get_pnl(self) -> float:
        """Calculate total PnL as the sum of realized + unrealized PnL."""
        return float(self.realized_pnl + self.get_unrealized_pnl_decimal())
    
    def get_unrealized_pnl_decimal(self, live_prices: Dict[str, float] = None) -> Decimal:
        """Calculate unrealized PnL from current open positions.
        
        Args:
            live_prices: Optional dict of live market prices {symbol: price}.
                        If provided, uses these instead of last fill prices.
        """
        unrealized_pnl = Decimal("0")
        
        for symbol, quantity in self.positions.items():
            if quantity > 0:  # Only calculate for positions we actually hold
                # Use live price if available, otherwise fall back to last fill price
                if live_prices and symbol in live_prices:
                    current_price = Decimal(str(live_prices[symbol]))
                    # Validate live price
                    if not self._validate_price(current_price, symbol):
                        current_price = self.last_price.get(symbol, Decimal("0"))
                else:
                    current_price = self.last_price.get(symbol, Decimal("0"))
                
                entry_price = self.entry_price.get(symbol, Decimal("0"))
                
                if current_price > 0 and entry_price > 0:
                    # Validate price is not stale (this affects accuracy but doesn't stop calculation)
                    if live_prices is None and self._is_price_stale(symbol):
                        # Price is stale - continue with calculation but log warning
                        # Note: In a real system, we'd want to log this via workflow logging
                        pass
                    
                    # Unrealized PnL = (current_price - entry_price) * quantity
                    position_pnl = (current_price - entry_price) * quantity
                    unrealized_pnl += position_pnl
        
        return unrealized_pnl

    @workflow.query
    def get_realized_pnl(self) -> float:
        """Calculate realized PnL from completed transactions (closed positions only)."""
        return float(self.realized_pnl)

    @workflow.query
    def get_unrealized_pnl(self) -> float:
        """Calculate unrealized PnL from current open positions."""
        return float(self.get_unrealized_pnl_decimal())
    
    @workflow.query 
    def get_unrealized_pnl_with_live_prices(self, live_prices: Dict[str, float]) -> float:
        """Calculate unrealized PnL using live market prices."""
        return float(self.get_unrealized_pnl_decimal(live_prices))
    
    @workflow.query
    def get_pnl_with_live_prices(self, live_prices: Dict[str, float]) -> float:
        """Calculate total PnL using live market prices.""" 
        return float(self.realized_pnl + self.get_unrealized_pnl_decimal(live_prices))

    @workflow.query
    def get_cash(self) -> float:
        return float(self.cash)
    
    @workflow.query
    def get_scraped_profits(self) -> float:
        """Return total profits that have been scraped/set aside."""
        return float(self.scraped_profits)

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
    
    @workflow.query
    def get_price_staleness_info(self) -> Dict[str, Any]:
        """Get information about price staleness for all positions."""
        staleness_info = {
            "stale_symbols": [],
            "fresh_symbols": [],
            "price_ages": {},
            "staleness_threshold_seconds": self.price_staleness_threshold
        }
        
        for symbol in self.positions.keys():
            age = self._get_price_age(symbol)
            staleness_info["price_ages"][symbol] = age
            
            if age == float('inf') or self._is_price_stale(symbol):
                staleness_info["stale_symbols"].append(symbol)
            else:
                staleness_info["fresh_symbols"].append(symbol)
        
        return staleness_info
    
    @workflow.query
    def get_risk_metrics_with_live_prices(self, live_prices: Dict[str, float]) -> Dict[str, float]:
        """Calculate current risk metrics using live market prices."""
        total_portfolio_value = float(self.cash)
        
        # Add position values using live prices
        for symbol, qty in self.positions.items():
            if live_prices and symbol in live_prices:
                price = live_prices[symbol]
            else:
                price = float(self.last_price.get(symbol, Decimal("0")))
            total_portfolio_value += float(qty) * price
        
        # Calculate position concentration
        position_concentrations = {}
        for symbol, qty in self.positions.items():
            if live_prices and symbol in live_prices:
                price = live_prices[symbol]
            else:
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