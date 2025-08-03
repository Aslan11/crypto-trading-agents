"""Tests for the judge agent and related components."""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta
from typing import Dict, List

from agents.workflows import JudgeAgentWorkflow, ExecutionLedgerWorkflow
from agents.prompt_manager import PromptManager, PromptComponent, PromptTemplate
from tools.performance_analysis import PerformanceAnalyzer, PerformanceReport


class TestJudgeAgentWorkflow:
    """Test the judge agent workflow functionality."""
    
    def test_initialization(self):
        """Test workflow initialization."""
        workflow = JudgeAgentWorkflow()
        
        assert workflow.evaluations == []
        assert len(workflow.prompt_versions) == 1
        assert workflow.current_prompt_version == 1
        assert workflow.last_evaluation_ts == 0
        
        # Check initial prompt version
        initial_version = workflow.prompt_versions[0]
        assert initial_version["version"] == 1
        assert initial_version["prompt_type"] == "execution_agent"
        assert initial_version["is_active"] is True
    
    def test_record_evaluation(self):
        """Test recording evaluations."""
        workflow = JudgeAgentWorkflow()
        
        evaluation = {
            "overall_score": 75.5,
            "component_scores": {"returns": 80, "risk": 70},
            "metrics": {"total_trades": 10}
        }
        
        workflow.record_evaluation(evaluation)
        
        assert len(workflow.evaluations) == 1
        assert workflow.evaluations[0]["overall_score"] == 75.5
        assert workflow.evaluations[0]["evaluation_id"] == 1
        assert "timestamp" in workflow.evaluations[0]
        assert workflow.last_evaluation_ts > 0
    
    def test_update_prompt_version(self):
        """Test prompt version updates."""
        workflow = JudgeAgentWorkflow()
        
        prompt_data = {
            "prompt_type": "execution_agent",
            "prompt_content": "New system prompt content",
            "description": "Enhanced risk management",
            "changes": ["Added conservative mode", "Increased safety checks"],
            "reason": "High drawdown detected"
        }
        
        workflow.update_prompt_version(prompt_data)
        
        assert len(workflow.prompt_versions) == 2
        assert workflow.current_prompt_version == 2
        
        # Check old version is deactivated
        assert workflow.prompt_versions[0]["is_active"] is False
        
        # Check new version
        new_version = workflow.prompt_versions[1]
        assert new_version["version"] == 2
        assert new_version["is_active"] is True
        assert new_version["prompt_content"] == "New system prompt content"
        assert new_version["description"] == "Enhanced risk management"
    
    def test_rollback_prompt(self):
        """Test prompt rollback functionality."""
        workflow = JudgeAgentWorkflow()
        
        # Add a new version
        workflow.update_prompt_version({
            "prompt_content": "Version 2",
            "description": "Test version"
        })
        
        # Add another version
        workflow.update_prompt_version({
            "prompt_content": "Version 3", 
            "description": "Another test version"
        })
        
        assert workflow.current_prompt_version == 3
        
        # Rollback to version 1
        workflow.rollback_prompt(1)
        
        assert workflow.current_prompt_version == 1
        assert workflow.prompt_versions[0]["is_active"] is True
        assert workflow.prompt_versions[1]["is_active"] is False
        assert workflow.prompt_versions[2]["is_active"] is False
    
    def test_get_evaluations(self):
        """Test getting evaluations with filtering."""
        workflow = JudgeAgentWorkflow()
        
        # Add multiple evaluations
        for i in range(5):
            eval_data = {"overall_score": 70 + i, "test_id": i}
            workflow.record_evaluation(eval_data)
        
        # Get all evaluations
        all_evals = workflow.get_evaluations(limit=10)
        assert len(all_evals) == 5
        
        # Check ordering (most recent first)
        scores = [e["overall_score"] for e in all_evals]
        assert scores == [74, 73, 72, 71, 70]  # Reverse order
        
        # Test limit
        limited_evals = workflow.get_evaluations(limit=3)
        assert len(limited_evals) == 3
    
    def test_get_performance_trend(self):
        """Test performance trend calculation."""
        workflow = JudgeAgentWorkflow()
        
        # No evaluations case
        trend = workflow.get_performance_trend(30)
        assert trend["trend"] == "stable"
        assert trend["avg_score"] == 0.0
        assert trend["evaluations_count"] == 0
        
        # Add evaluations with improving trend
        base_time = int(datetime.now(timezone.utc).timestamp())
        scores = [60, 65, 70, 75, 80]  # Improving
        
        for i, score in enumerate(scores):
            eval_data = {
                "overall_score": score,
                "timestamp": base_time + (i * 3600)  # 1 hour apart
            }
            workflow.evaluations.append(eval_data)
        
        trend = workflow.get_performance_trend(30)
        assert trend["trend"] == "improving"
        assert trend["avg_score"] == 70.0  # Average of scores
        assert trend["evaluations_count"] == 5
        assert trend["improvement"] > 0
    
    def test_should_trigger_evaluation(self):
        """Test evaluation timing logic."""
        workflow = JudgeAgentWorkflow()
        
        # No previous evaluation
        assert workflow.should_trigger_evaluation(4) is True
        
        # Recent evaluation
        workflow.last_evaluation_ts = int(datetime.now(timezone.utc).timestamp()) - 3600  # 1 hour ago
        assert workflow.should_trigger_evaluation(4) is False  # 4 hour cooldown
        
        # Old evaluation
        workflow.last_evaluation_ts = int(datetime.now(timezone.utc).timestamp()) - 5 * 3600  # 5 hours ago
        assert workflow.should_trigger_evaluation(4) is True


class TestExecutionLedgerWorkflowEnhancements:
    """Test the enhanced execution ledger workflow."""
    
    def test_transaction_history_tracking(self):
        """Test transaction history is properly tracked."""
        workflow = ExecutionLedgerWorkflow()
        
        fill_data = {
            "side": "BUY",
            "symbol": "BTC/USD",
            "qty": 0.1,
            "fill_price": 50000.0,
            "cost": 5000.0
        }
        
        workflow.record_fill(fill_data)
        
        assert len(workflow.transaction_history) == 1
        
        tx = workflow.transaction_history[0]
        assert tx["side"] == "BUY"
        assert tx["symbol"] == "BTC/USD"
        assert tx["quantity"] == 0.1
        assert tx["fill_price"] == 50000.0
        assert tx["cost"] == 5000.0
        assert "timestamp" in tx
        assert "cash_before" in tx
        assert "position_before" in tx
    
    def test_get_transaction_history(self):
        """Test transaction history retrieval."""
        workflow = ExecutionLedgerWorkflow()
        
        # Add multiple transactions
        for i in range(5):
            fill_data = {
                "side": "BUY" if i % 2 == 0 else "SELL",
                "symbol": f"SYMBOL{i}",
                "qty": 0.1 + i * 0.1,
                "fill_price": 1000.0 + i * 100,
                "cost": (0.1 + i * 0.1) * (1000.0 + i * 100)
            }
            workflow.record_fill(fill_data)
        
        # Test getting all transactions
        all_txs = workflow.get_transaction_history()
        assert len(all_txs) == 5
        
        # Test with limit
        limited_txs = workflow.get_transaction_history(limit=3)
        assert len(limited_txs) == 3
        
        # Test with timestamp filter (get none from future)
        future_ts = int(datetime.now(timezone.utc).timestamp()) + 3600
        future_txs = workflow.get_transaction_history(since_ts=future_ts)
        assert len(future_txs) == 0
    
    def test_get_performance_metrics(self):
        """Test performance metrics calculation."""
        workflow = ExecutionLedgerWorkflow()
        
        # No transactions case
        metrics = workflow.get_performance_metrics(30)
        assert metrics["total_trades"] == 0
        assert metrics["win_rate"] == 0.0
        assert metrics["total_pnl"] == 0.0
        
        # Add some transactions
        transactions = [
            {"side": "BUY", "symbol": "BTC/USD", "qty": 0.1, "fill_price": 50000, "cost": 5000},
            {"side": "SELL", "symbol": "BTC/USD", "qty": 0.05, "fill_price": 52000, "cost": 2600},
            {"side": "BUY", "symbol": "ETH/USD", "qty": 1.0, "fill_price": 3000, "cost": 3000},
        ]
        
        for tx in transactions:
            workflow.record_fill(tx)
        
        metrics = workflow.get_performance_metrics(30)
        assert metrics["total_trades"] == 3
        assert isinstance(metrics["avg_trade_pnl"], float)
        assert isinstance(metrics["max_drawdown"], float)
    
    def test_get_risk_metrics(self):
        """Test risk metrics calculation."""
        workflow = ExecutionLedgerWorkflow()
        
        # Add some positions
        workflow.record_fill({
            "side": "BUY", "symbol": "BTC/USD", "qty": 0.1, 
            "fill_price": 50000, "cost": 5000
        })
        workflow.record_fill({
            "side": "BUY", "symbol": "ETH/USD", "qty": 1.0,
            "fill_price": 3000, "cost": 3000
        })
        
        risk_metrics = workflow.get_risk_metrics()
        
        assert "total_portfolio_value" in risk_metrics
        assert "cash_ratio" in risk_metrics
        assert "max_position_concentration" in risk_metrics
        assert "num_positions" in risk_metrics
        assert risk_metrics["num_positions"] == 2
        assert 0 <= risk_metrics["cash_ratio"] <= 1
        assert 0 <= risk_metrics["max_position_concentration"] <= 1


class TestPromptManager:
    """Test the prompt manager functionality."""
    
    def test_component_creation(self):
        """Test creating prompt components."""
        component = PromptComponent(
            name="test_component",
            content="Test content",
            priority=100,
            conditions={"risk_mode": "conservative"}
        )
        
        assert component.name == "test_component"
        assert component.content == "Test content"
        assert component.priority == 100
        assert component.conditions == {"risk_mode": "conservative"}
    
    def test_template_rendering(self):
        """Test prompt template rendering."""
        components = [
            PromptComponent("role", "You are a trading agent.", priority=1000),
            PromptComponent("rules", "Follow these rules: ...", priority=900),
            PromptComponent("risk_conservative", "Be very conservative.", 
                          priority=800, conditions={"risk_mode": "conservative"}),
            PromptComponent("risk_aggressive", "Take more risks.", 
                          priority=800, conditions={"risk_mode": "aggressive"})
        ]
        
        template = PromptTemplate(
            name="test_template",
            description="Test template",
            components=components
        )
        
        # Test default rendering (no conditions)
        default_prompt = template.render()
        assert "You are a trading agent." in default_prompt
        assert "Follow these rules:" in default_prompt
        assert "Be very conservative." not in default_prompt
        assert "Take more risks." not in default_prompt
        
        # Test conservative context
        conservative_prompt = template.render({"risk_mode": "conservative"})
        assert "Be very conservative." in conservative_prompt
        assert "Take more risks." not in conservative_prompt
        
        # Test aggressive context
        aggressive_prompt = template.render({"risk_mode": "aggressive"})
        assert "Take more risks." in aggressive_prompt
        assert "Be very conservative." not in aggressive_prompt
    
    def test_prompt_manager_initialization(self):
        """Test prompt manager initialization."""
        manager = PromptManager()
        
        # Check default components exist
        assert "role_definition" in manager.default_components
        assert "operational_workflow" in manager.default_components
        assert "decision_framework" in manager.default_components
        assert "risk_management" in manager.default_components
        
        # Check default templates exist
        assert "execution_agent_standard" in manager.templates
        assert "execution_agent_conservative" in manager.templates
        assert "execution_agent_performance" in manager.templates
    
    @pytest.mark.asyncio
    async def test_get_current_prompt_fallback(self):
        """Test prompt fallback when temporal unavailable."""
        manager = PromptManager(temporal_client=None)
        
        prompt = await manager.get_current_prompt("execution_agent")
        
        # Should get default template
        assert len(prompt) > 0
        assert "autonomous portfolio management agent" in prompt.lower()
    
    def test_generate_prompt_variants(self):
        """Test generating prompt variants based on performance."""
        manager = PromptManager()
        
        # High drawdown performance data
        performance_data = {
            "max_drawdown": 0.20,  # 20% drawdown
            "win_rate": 0.45,
            "risk_management_score": 60.0
        }
        
        variants = manager.generate_prompt_variants(
            "execution_agent_standard",
            performance_data
        )
        
        assert len(variants) > 0
        
        # Should generate conservative variant
        variant_names = [v[0] for v in variants]
        assert "conservative_risk" in variant_names
        
        # Check variant content
        conservative_variant = next(v for v in variants if v[0] == "conservative_risk")
        assert "conservative" in conservative_variant[2].lower()


class TestPerformanceAnalyzer:
    """Test the performance analysis functionality."""
    
    def test_sharpe_ratio_calculation(self):
        """Test Sharpe ratio calculation."""
        analyzer = PerformanceAnalyzer()
        
        # Test with no returns
        assert analyzer.calculate_sharpe_ratio([]) == 0.0
        
        # Test with single return
        assert analyzer.calculate_sharpe_ratio([0.01]) == 0.0
        
        # Test with positive returns
        returns = [0.01, 0.02, -0.005, 0.015, 0.008]
        sharpe = analyzer.calculate_sharpe_ratio(returns)
        assert isinstance(sharpe, float)
    
    def test_max_drawdown_calculation(self):
        """Test maximum drawdown calculation."""
        analyzer = PerformanceAnalyzer()
        
        # Test with no values
        assert analyzer.calculate_max_drawdown([]) == 0.0
        
        # Test with increasing values (no drawdown)
        increasing_values = [100, 110, 120, 130]
        assert analyzer.calculate_max_drawdown(increasing_values) == 0.0
        
        # Test with drawdown
        values_with_dd = [100, 120, 110, 90, 95, 105]
        max_dd = analyzer.calculate_max_drawdown(values_with_dd)
        assert max_dd > 0
        assert max_dd <= 1.0  # Should be a percentage
    
    def test_win_metrics_calculation(self):
        """Test win rate and profit metrics calculation."""
        analyzer = PerformanceAnalyzer()
        
        transactions = [
            {"symbol": "BTC/USD", "side": "BUY", "quantity": 0.1, "fill_price": 50000, "timestamp": 1000},
            {"symbol": "BTC/USD", "side": "SELL", "quantity": 0.1, "fill_price": 52000, "timestamp": 2000},
            {"symbol": "ETH/USD", "side": "BUY", "quantity": 1.0, "fill_price": 3000, "timestamp": 3000},
            {"symbol": "ETH/USD", "side": "SELL", "quantity": 1.0, "fill_price": 2900, "timestamp": 4000},
        ]
        
        win_rate, avg_win, avg_loss, profit_factor = analyzer.calculate_win_metrics(transactions)
        
        assert 0 <= win_rate <= 1
        assert isinstance(avg_win, float)
        assert isinstance(avg_loss, float)
        assert isinstance(profit_factor, float)
    
    def test_performance_report_generation(self):
        """Test comprehensive performance report generation."""
        analyzer = PerformanceAnalyzer()
        
        # Sample data
        transactions = [
            {"symbol": "BTC/USD", "side": "BUY", "quantity": 0.1, "fill_price": 50000, 
             "timestamp": int(datetime.now(timezone.utc).timestamp()) - 86400, "cost": 5000},
            {"symbol": "BTC/USD", "side": "SELL", "quantity": 0.1, "fill_price": 52000,
             "timestamp": int(datetime.now(timezone.utc).timestamp()), "cost": 5200}
        ]
        
        performance_metrics = {
            "total_pnl": 200.0,
            "max_drawdown": 0.05,
            "total_trades": 2
        }
        
        risk_metrics = {
            "total_portfolio_value": 252000.0,
            "cash_ratio": 0.95,
            "max_position_concentration": 0.1
        }
        
        report = analyzer.generate_performance_report(
            transactions, performance_metrics, risk_metrics
        )
        
        assert isinstance(report, PerformanceReport)
        assert report.total_trades == 2
        assert report.total_return > 0  # Should be positive from profitable trade
        assert 0 <= report.decision_quality_score <= 100
        assert 0 <= report.risk_management_score <= 100
        assert 0 <= report.consistency_score <= 100
        assert report.overall_grade in ["A", "B", "C", "D", "F"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])