"""Performance analysis tools for trading evaluation."""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timezone
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PerformanceReport:
    """Comprehensive performance analysis report."""
    
    # Time period
    start_date: datetime
    end_date: datetime
    
    # Return metrics
    total_return: float
    annualized_return: float
    volatility: float
    sharpe_ratio: float
    max_drawdown: float
    
    # Trading metrics
    total_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    
    # Risk metrics
    var_95: float  # Value at Risk 95%
    max_position_size: float
    avg_position_size: float
    
    # Qualitative scores (0-100)
    decision_quality_score: float
    risk_management_score: float
    consistency_score: float
    
    # Overall performance grade
    overall_grade: str  # A, B, C, D, F


class PerformanceAnalyzer:
    """Analyzes trading performance and generates evaluation reports."""
    
    def __init__(self):
        self.risk_free_rate = 0.02  # 2% annual risk-free rate
    
    def calculate_returns(self, transactions: List[Dict]) -> List[float]:
        """Calculate period returns from transaction history."""
        if not transactions:
            return []
        
        # Sort transactions by timestamp
        sorted_txs = sorted(transactions, key=lambda x: x["timestamp"])
        
        returns = []
        prev_portfolio_value = sorted_txs[0]["cash_before"]
        
        for tx in sorted_txs:
            # Simplified return calculation
            current_value = tx["cash_before"] + tx.get("position_value", 0)
            if prev_portfolio_value > 0:
                period_return = (current_value - prev_portfolio_value) / prev_portfolio_value
                returns.append(period_return)
            prev_portfolio_value = current_value
        
        return returns
    
    def calculate_sharpe_ratio(self, returns: List[float]) -> float:
        """Calculate Sharpe ratio from returns."""
        if not returns or len(returns) < 2:
            return 0.0
        
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 0
        
        if std_dev == 0:
            return 0.0
        
        # Annualize assuming daily returns
        annual_return = mean_return * 252
        annual_std = std_dev * math.sqrt(252)
        
        return (annual_return - self.risk_free_rate) / annual_std
    
    def calculate_max_drawdown(self, portfolio_values: List[float]) -> float:
        """Calculate maximum drawdown from portfolio value series."""
        if not portfolio_values:
            return 0.0
        
        max_dd = 0.0
        peak = portfolio_values[0]
        
        for value in portfolio_values:
            if value > peak:
                peak = value
            
            drawdown = (peak - value) / peak if peak > 0 else 0
            max_dd = max(max_dd, drawdown)
        
        return max_dd
    
    def calculate_win_metrics(self, transactions: List[Dict]) -> Tuple[float, float, float, float]:
        """Calculate win rate, average win, average loss, and profit factor."""
        if not transactions:
            return 0.0, 0.0, 0.0, 0.0
        
        wins = []
        losses = []
        
        # Group transactions by symbol to calculate trade PnL
        position_tracker = {}
        
        for tx in sorted(transactions, key=lambda x: x["timestamp"]):
            symbol = tx["symbol"]
            side = tx["side"]
            qty = tx["quantity"]
            price = tx["fill_price"]
            
            if symbol not in position_tracker:
                position_tracker[symbol] = {"qty": 0, "avg_cost": 0}
            
            pos = position_tracker[symbol]
            
            if side == "BUY":
                # Average down cost basis
                total_cost = pos["qty"] * pos["avg_cost"] + qty * price
                pos["qty"] += qty
                pos["avg_cost"] = total_cost / pos["qty"] if pos["qty"] > 0 else price
            else:  # SELL
                if pos["qty"] > 0:
                    # Calculate realized PnL
                    pnl = qty * (price - pos["avg_cost"])
                    if pnl > 0:
                        wins.append(pnl)
                    else:
                        losses.append(abs(pnl))
                    
                    pos["qty"] -= qty
                    if pos["qty"] <= 0:
                        pos["qty"] = 0
                        pos["avg_cost"] = 0
        
        total_trades = len(wins) + len(losses)
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        profit_factor = sum(wins) / sum(losses) if losses and sum(losses) > 0 else float('inf') if wins else 0.0
        
        return win_rate, avg_win, avg_loss, profit_factor
    
    def calculate_var_95(self, returns: List[float]) -> float:
        """Calculate 95% Value at Risk."""
        if not returns or len(returns) < 20:  # Need sufficient data
            return 0.0
        
        sorted_returns = sorted(returns)
        var_index = int(len(sorted_returns) * 0.05)  # 5th percentile
        return abs(sorted_returns[var_index])
    
    def score_decision_quality(self, transactions: List[Dict], market_data: Optional[Dict] = None) -> float:
        """Score decision quality based on timing and rationale (0-100)."""
        if not transactions:
            return 50.0  # Neutral score for no activity
        
        # Simplified scoring based on trade frequency and consistency
        total_trades = len(transactions)
        
        # Penalize overtrading (>10 trades/day on average)
        time_span_days = max(1, (transactions[-1]["timestamp"] - transactions[0]["timestamp"]) / (24 * 60 * 60))
        trade_frequency = total_trades / time_span_days
        
        frequency_score = 100.0
        if trade_frequency > 10:
            frequency_score = max(20.0, 100.0 - (trade_frequency - 10) * 5)
        elif trade_frequency < 0.1:  # Too little trading
            frequency_score = max(20.0, 100.0 - (0.1 - trade_frequency) * 200)
        
        # Score based on position sizing consistency
        position_sizes = [tx.get("quantity", 0) for tx in transactions]
        if position_sizes:
            avg_size = sum(position_sizes) / len(position_sizes)
            size_variance = sum((size - avg_size) ** 2 for size in position_sizes) / len(position_sizes)
            size_std = math.sqrt(size_variance)
            consistency_ratio = size_std / avg_size if avg_size > 0 else 1.0
            
            consistency_score = max(0.0, 100.0 - consistency_ratio * 100)
        else:
            consistency_score = 50.0
        
        # Weighted average of scoring components
        decision_score = (frequency_score * 0.6 + consistency_score * 0.4)
        return min(100.0, max(0.0, decision_score))
    
    def score_risk_management(self, performance_metrics: Dict, risk_metrics: Dict) -> float:
        """Score risk management effectiveness (0-100)."""
        score = 100.0
        
        # Penalize high drawdown
        max_drawdown = performance_metrics.get("max_drawdown", 0.0)
        if max_drawdown > 0.2:  # More than 20% drawdown
            score -= (max_drawdown - 0.2) * 200  # -40 points for 40% drawdown
        
        # Penalize high concentration
        max_concentration = risk_metrics.get("max_position_concentration", 0.0)
        if max_concentration > 0.3:  # More than 30% in single position
            score -= (max_concentration - 0.3) * 100
        
        # Reward appropriate cash levels
        cash_ratio = risk_metrics.get("cash_ratio", 0.0)
        if cash_ratio < 0.05:  # Less than 5% cash
            score -= 20
        elif cash_ratio > 0.8:  # More than 80% cash (too conservative)
            score -= 15
        
        return min(100.0, max(0.0, score))
    
    def score_consistency(self, returns: List[float]) -> float:
        """Score consistency of returns (0-100)."""
        if not returns or len(returns) < 5:
            return 50.0
        
        # Calculate return consistency metrics
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)
        
        # Score based on coefficient of variation
        if mean_return != 0:
            cv = abs(std_dev / mean_return)
            consistency_score = max(0.0, 100.0 - cv * 50)
        else:
            consistency_score = 50.0  # Neutral for zero returns
        
        return min(100.0, consistency_score)
    
    def calculate_overall_grade(self, performance_report: PerformanceReport) -> str:
        """Calculate overall performance grade A-F."""
        # Weighted scoring
        weights = {
            "returns": 0.3,
            "risk": 0.3,
            "quality": 0.2,
            "consistency": 0.2
        }
        
        # Normalize metrics to 0-100 scale
        return_score = min(100.0, max(0.0, (performance_report.annualized_return + 1) * 50))
        risk_score = performance_report.risk_management_score
        quality_score = performance_report.decision_quality_score
        consistency_score = performance_report.consistency_score
        
        overall_score = (
            return_score * weights["returns"] +
            risk_score * weights["risk"] +
            quality_score * weights["quality"] +
            consistency_score * weights["consistency"]
        )
        
        if overall_score >= 90:
            return "A"
        elif overall_score >= 80:
            return "B"
        elif overall_score >= 70:
            return "C"
        elif overall_score >= 60:
            return "D"
        else:
            return "F"
    
    def generate_performance_report(
        self,
        transactions: List[Dict],
        performance_metrics: Dict,
        risk_metrics: Dict,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> PerformanceReport:
        """Generate comprehensive performance report."""
        
        if not start_date and transactions:
            start_date = datetime.fromtimestamp(transactions[0]["timestamp"], tz=timezone.utc)
        if not end_date and transactions:
            end_date = datetime.fromtimestamp(transactions[-1]["timestamp"], tz=timezone.utc)
        
        if not start_date:
            start_date = datetime.now(tz=timezone.utc)
        if not end_date:
            end_date = datetime.now(tz=timezone.utc)
        
        # Calculate return metrics
        returns = self.calculate_returns(transactions)
        
        total_return = performance_metrics.get("total_pnl", 0.0) / 250000.0  # Assuming 250k initial
        days = max(1, (end_date - start_date).days)
        annualized_return = (1 + total_return) ** (365.25 / days) - 1 if days > 0 else 0.0
        
        volatility = math.sqrt(sum(r**2 for r in returns) / len(returns)) if returns else 0.0
        sharpe_ratio = self.calculate_sharpe_ratio(returns)
        max_drawdown = performance_metrics.get("max_drawdown", 0.0)
        
        # Trading metrics
        win_rate, avg_win, avg_loss, profit_factor = self.calculate_win_metrics(transactions)
        
        # Risk metrics
        var_95 = self.calculate_var_95(returns)
        position_sizes = [tx.get("quantity", 0) for tx in transactions]
        max_position_size = max(position_sizes) if position_sizes else 0.0
        avg_position_size = sum(position_sizes) / len(position_sizes) if position_sizes else 0.0
        
        # Qualitative scores
        decision_quality_score = self.score_decision_quality(transactions)
        risk_management_score = self.score_risk_management(performance_metrics, risk_metrics)
        consistency_score = self.score_consistency(returns)
        
        # Create report
        report = PerformanceReport(
            start_date=start_date,
            end_date=end_date,
            total_return=total_return,
            annualized_return=annualized_return,
            volatility=volatility,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_drawdown,
            total_trades=len(transactions),
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            var_95=var_95,
            max_position_size=max_position_size,
            avg_position_size=avg_position_size,
            decision_quality_score=decision_quality_score,
            risk_management_score=risk_management_score,
            consistency_score=consistency_score,
            overall_grade=""
        )
        
        # Calculate overall grade
        report.overall_grade = self.calculate_overall_grade(report)
        
        return report


def format_performance_report(report: PerformanceReport) -> str:
    """Format performance report as human-readable text."""
    
    period = f"{report.start_date.strftime('%Y-%m-%d')} to {report.end_date.strftime('%Y-%m-%d')}"
    
    return f"""
TRADING PERFORMANCE REPORT
{'=' * 50}
Period: {period}
Overall Grade: {report.overall_grade}

RETURN METRICS:
• Total Return: {report.total_return:.2%}
• Annualized Return: {report.annualized_return:.2%}
• Volatility: {report.volatility:.2%}
• Sharpe Ratio: {report.sharpe_ratio:.2f}
• Max Drawdown: {report.max_drawdown:.2%}

TRADING METRICS:
• Total Trades: {report.total_trades}
• Win Rate: {report.win_rate:.2%}
• Average Win: ${report.avg_win:.2f}
• Average Loss: ${report.avg_loss:.2f}
• Profit Factor: {report.profit_factor:.2f}

RISK METRICS:
• VaR 95%: {report.var_95:.2%}
• Max Position Size: {report.max_position_size:.2f}
• Avg Position Size: {report.avg_position_size:.2f}

QUALITATIVE SCORES (0-100):
• Decision Quality: {report.decision_quality_score:.1f}
• Risk Management: {report.risk_management_score:.1f}
• Consistency: {report.consistency_score:.1f}
"""