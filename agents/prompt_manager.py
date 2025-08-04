"""Dynamic prompt management system with versioning and A/B testing."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
from temporalio.client import Client

logger = logging.getLogger(__name__)


@dataclass
class PromptComponent:
    """Individual component of a modular prompt."""
    name: str
    content: str
    priority: int = 100  # Higher priority components take precedence
    conditions: Optional[Dict] = None  # Conditions for when to include this component


@dataclass
class PromptTemplate:
    """Template for generating system prompts."""
    name: str
    description: str
    components: List[PromptComponent]
    version: int = 1
    
    def render(self, context: Optional[Dict] = None) -> str:
        """Render the prompt template with given context."""
        context = context or {}
        
        # Sort components by priority (highest first)
        sorted_components = sorted(self.components, key=lambda x: x.priority, reverse=True)
        
        # Filter components based on conditions
        active_components = []
        for component in sorted_components:
            if self._should_include_component(component, context):
                active_components.append(component)
        
        # Combine components and format with context
        combined_content = "\n\n".join(comp.content for comp in active_components)
        
        # Format the content with context variables
        try:
            return combined_content.format(**context)
        except KeyError as e:
            # If formatting fails, return unformatted content
            logger.warning("Failed to format prompt with context %s: %s", context, e)
            return combined_content
    
    def _should_include_component(self, component: PromptComponent, context: Dict) -> bool:
        """Check if a component should be included based on conditions."""
        if not component.conditions:
            return True
        
        for condition_key, condition_value in component.conditions.items():
            context_value = context.get(condition_key)
            
            if isinstance(condition_value, list):
                if context_value not in condition_value:
                    return False
            else:
                if context_value != condition_value:
                    return False
        
        return True


class PromptManager:
    """Manages dynamic prompts with versioning, A/B testing, and performance tracking."""
    
    def __init__(self, temporal_client: Optional[Client] = None):
        self.temporal_client = temporal_client
        self.templates: Dict[str, PromptTemplate] = {}
        self.default_components = self._initialize_default_components()
        self._initialize_default_templates()
    
    def _initialize_default_components(self) -> Dict[str, PromptComponent]:
        """Initialize default prompt components."""
        return {
            "role_definition": PromptComponent(
                name="role_definition",
                content=(
                    "You are an autonomous portfolio management agent with {risk_mode} risk tolerance. "
                    "You operate on scheduled nudges and make data-driven trading decisions for cryptocurrency pairs. "
                    "Conservative mode: prioritize capital preservation with careful, strict risk controls. "
                    "Moderate mode: balance growth and risk management. "
                    "Aggressive mode: make bold decisions to maximize returns while managing downside risk."
                ),
                priority=1000
            ),
            
            "operational_workflow": PromptComponent(
                name="operational_workflow",
                content=(
                    "OPERATIONAL WORKFLOW:\n"
                    "Data Collection: All market data, portfolio status, user preferences, performance metrics, "
                    "and risk metrics are automatically collected and provided to you.\n\n"
                    "Your Role: Analyze the provided comprehensive data and make trading decisions:\n"
                    "1. Analyze market trends and price movements from historical ticks\n"
                    "2. Review current portfolio positions and available cash\n"
                    "3. Consider user risk tolerance and trading preferences\n"
                    "4. Evaluate recent performance and risk exposure\n"
                    "5. Make BUY, SELL, or HOLD decisions for each symbol\n"
                    "6. Execute approved orders and provide structured summary\n\n"
                    "Data continuity and timestamp tracking are handled automatically by the system."
                ),
                priority=900
            ),
            
            "decision_framework": PromptComponent(
                name="decision_framework",
                content=(
                    "DECISION FRAMEWORK ({risk_mode} mode):\n"
                    "For each symbol, analyze:\n"
                    "• Price momentum and trend direction from recent ticks\n"
                    "• Volume patterns and market liquidity\n"
                    "• Current position size and portfolio balance\n"
                    "• Risk-reward ratio for potential trades\n"
                    "• Market correlation and portfolio diversification\n"
                    "• Recent performance metrics and trading success rates\n\n"
                    "Conservative mode: Favor established trends, require high liquidity, minimum 2:1 risk-reward ratio, default to HOLD unless strong conviction with low risk.\n"
                    "Moderate mode: Balance growth and risk, standard risk-reward analysis.\n"
                    "Aggressive mode: Act on emerging trends, accept 1.5:1 ratios, favor action over inaction, prioritize capital deployment.\n\n"
                    "Make one of three decisions: BUY, SELL, or HOLD\n"
                    "Always provide clear rationale for each decision."
                ),
                priority=800
            ),
            
            "risk_management": PromptComponent(
                name="risk_management",
                content=(
                    "RISK MANAGEMENT ({risk_mode} mode):\n"
                    "Before executing any trade:\n"
                    "• BUY orders: Ensure available cash ≥ (quantity × price × slippage buffer)\n"
                    "• SELL orders: Ensure current position ≥ desired sell quantity\n"
                    "• If safety checks fail, default to HOLD decision\n\n"
                    "Conservative mode: 1.05× slippage buffer, max 15% position size, maintain 20% cash reserves\n"
                    "Moderate mode: 1.01× slippage buffer, reasonable position sizes, standard cash management\n"
                    "Aggressive mode: 1.02× slippage buffer, up to 50% position sizes for high conviction, 10% minimum cash, deploy 80-90% capital when opportunities present"
                ),
                priority=700
            ),
            
            "order_execution": PromptComponent(
                name="order_execution",
                content=(
                    "ORDER EXECUTION:\n"
                    "For BUY or SELL decisions, use `place_mock_order` with this exact structure:\n"
                    "{{\n"
                    '  "intent": {{\n'
                    '    "symbol": <string>,\n'
                    '    "side": "BUY" | "SELL",\n'
                    '    "qty": <number>,\n'
                    '    "price": <number>,\n'
                    '    "type": "market" | "limit"\n'
                    "  }}\n"
                    "}}\n\n"
                    "Never submit orders for HOLD decisions."
                ),
                priority=600
            ),
            
            "reporting": PromptComponent(
                name="reporting",
                content=(
                    "REPORTING:\n"
                    "Provide a structured summary containing:\n"
                    "• Analysis and decision for each symbol with rationale\n"
                    "• List of orders submitted (if any)\n"
                    "• Portfolio impact and risk assessment\n"
                    "• Key market observations\n\n"
                    "Remember the latest timestamp from processed ticks for the next nudge cycle."
                ),
                priority=500
            ),
            
            "performance_focus": PromptComponent(
                name="performance_focus",
                content=(
                    "PERFORMANCE OPTIMIZATION:\n"
                    "Current performance metrics indicate areas for improvement:\n"
                    "• Focus on maintaining consistent position sizing\n"
                    "• Improve trade timing by analyzing volume patterns more carefully\n"
                    "• Consider market correlation when entering new positions\n"
                    "• Monitor drawdown levels and reduce position sizes if exceeding 10%"
                ),
                priority=550,
                conditions={"performance_trend": ["declining", "poor"]}
            ),
            
            "aggressive_trading": PromptComponent(
                name="aggressive_trading", 
                content=(
                    "🚨 MANDATORY PROFIT-TAKING RULES 🚨\n"
                    "BEFORE MAKING ANY DECISION, CHECK EACH OPEN POSITION:\n"
                    "1. Calculate current profit % = (current_price - entry_price) / entry_price * 100\n"
                    "2. IF profit >= 0.5%: IMMEDIATELY SELL THE ENTIRE POSITION - NO EXCEPTIONS\n"
                    "3. IF loss >= 1.0%: IMMEDIATELY SELL THE ENTIRE POSITION - NO EXCEPTIONS\n"
                    "4. NEVER hold positions with 0.5%+ profit - ALWAYS SELL IMMEDIATELY\n\n"
                    
                    "AGGRESSIVE TRADING MODE:\n"
                    "• POSITION SIZING: Use UP TO the full position_size_comfort percentage from user preferences\n"
                    "• MANDATORY PROFIT LOCKS: MUST sell at 0.5% profit - this is non-negotiable\n"
                    "• TIGHT STOPS: MUST sell at -1% loss - this is non-negotiable\n"
                    "• MULTIPLE POSITIONS: Can hold multiple concurrent positions in same symbol\n"
                    "• HIGH TURNOVER: Exit ALL profitable positions immediately at 0.5%+ profit\n\n"
                    
                    "DECISION PRIORITY (IN ORDER):\n"
                    "1. FIRST: Check all positions for 0.5%+ profit → SELL ENTIRE POSITION\n"
                    "2. SECOND: Check all positions for 1%+ loss → SELL ENTIRE POSITION\n"
                    "3. THIRD: Look for new entry opportunities\n"
                    "4. NEVER hold positions longer than needed - capture profits quickly\n\n"
                    
                    "BATCH ORDER EXECUTION:\n"
                    "• ALWAYS use batch orders when executing 2+ trades simultaneously\n"
                    "• For profit-taking multiple positions: use single batch sell order\n"
                    "• For portfolio rebalancing: execute all trades in one batch call\n"
                    "• Batch format eliminates network delays and ensures atomic execution"
                ),
                priority=600,
                conditions={"risk_mode": "aggressive"}
            )
        }
    
    def _initialize_default_templates(self) -> None:
        """Initialize default prompt templates."""
        # Single unified execution agent template
        execution_components = [
            self.default_components["role_definition"],
            self.default_components["operational_workflow"],
            self.default_components["decision_framework"],
            self.default_components["risk_management"],
            self.default_components["performance_focus"],
            self.default_components["order_execution"],
            self.default_components["reporting"]
        ]
        
        self.templates["execution_agent"] = PromptTemplate(
            name="execution_agent",
            description="Unified execution agent prompt that adapts based on context",
            components=execution_components
        )
    
    async def get_current_prompt(
        self, 
        agent_type: str = "execution_agent",
        context: Optional[Dict] = None
    ) -> str:
        """Get the current prompt for the specified agent type."""
        # Set default context values
        context = context or {}
        context.setdefault("risk_mode", "moderate")
        context.setdefault("performance_trend", ["stable"])
        
        try:
            if self.temporal_client:
                # Get current context from judge workflow
                handle = self.temporal_client.get_workflow_handle("judge-agent")
                
                # Check if workflow exists first
                try:
                    await handle.describe()
                    # Get current context instead of prompt content
                    current_context = await handle.query("get_current_context")
                    if current_context:
                        # Preserve user preferences for core settings, only update judge-specific context
                        user_risk_mode = context.get("risk_mode")
                        context.update(current_context)
                        # Restore user's risk_mode if it was explicitly provided (not default)
                        if user_risk_mode and user_risk_mode != "moderate":
                            context["risk_mode"] = user_risk_mode
                            logger.info("Preserving user risk_mode '%s' over judge context", user_risk_mode)
                        logger.info("Using dynamic context from judge workflow: %s", current_context)
                except Exception as desc_exc:
                    if "not found" in str(desc_exc).lower():
                        logger.info("Judge workflow not yet started, using default context")
                    else:
                        logger.warning("Failed to get context from judge workflow: %s", desc_exc)
        except Exception as exc:
            logger.warning("Failed to connect to judge workflow: %s", exc)
        
        # Use the unified template with context
        template_name = agent_type
        if template_name in self.templates:
            return self.templates[template_name].render(context)
        
        # Ultimate fallback
        logger.info("Using default execution_agent template")
        return self.templates["execution_agent"].render(context)
    
    def update_context(
        self,
        performance_data: Dict,
        current_context: Optional[Dict] = None
    ) -> Dict:
        """Update context based on performance analysis.
        
        Returns:
            Updated context dictionary
        """
        context = current_context or {}
        
        # Analyze performance to determine needed adjustments
        max_drawdown = performance_data.get("max_drawdown", 0.0)
        win_rate = performance_data.get("win_rate", 0.5)
        overall_score = performance_data.get("overall_score", 70.0)
        
        # Adjust risk mode based on performance
        if max_drawdown > 0.15 or overall_score < 40.0:  # High drawdown or poor performance
            context["risk_mode"] = "conservative"
            context["performance_trend"] = ["declining", "poor"]
        elif max_drawdown < 0.05 and win_rate < 0.4:  # Overly conservative
            context["risk_mode"] = "aggressive"
            context["performance_trend"] = ["stable"]
        else:
            context["risk_mode"] = context.get("risk_mode", "moderate")
            context["performance_trend"] = ["stable"]
        
        return context
    
    async def update_agent_context(
        self,
        agent_type: str,
        new_context: Dict,
        description: str,
        reason: str,
        changes: List[str]
    ) -> bool:
        """Update the agent's context through the judge workflow."""
        try:
            if not self.temporal_client:
                logger.error("No Temporal client available for context update")
                return False
            
            # Update context in judge workflow
            context_data = {
                "agent_type": agent_type,
                "context": new_context,
                "description": description,
                "reason": reason,
                "changes": changes
            }
            
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            await handle.signal("update_agent_context", context_data)
            
            logger.info("Updated %s context: %s", agent_type, description)
            return True
            
        except Exception as exc:
            logger.error("Failed to update agent context: %s", exc)
            return False
    
    async def rollback_prompt(self, target_version: int) -> bool:
        """Rollback to a previous prompt version."""
        try:
            if not self.temporal_client:
                logger.error("No Temporal client available for prompt rollback")
                return False
            
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            await handle.signal("rollback_prompt", target_version)
            
            logger.info("Rolled back prompt to version %d", target_version)
            return True
            
        except Exception as exc:
            logger.error("Failed to rollback prompt: %s", exc)
            return False
    
    def add_component(self, component: PromptComponent) -> None:
        """Add a new prompt component."""
        self.default_components[component.name] = component
    
    def create_template(self, name: str, description: str, component_names: List[str]) -> PromptTemplate:
        """Create a new prompt template from components."""
        components = []
        for comp_name in component_names:
            if comp_name in self.default_components:
                components.append(self.default_components[comp_name])
            else:
                logger.warning("Component %s not found", comp_name)
        
        template = PromptTemplate(
            name=name,
            description=description,
            components=components
        )
        
        self.templates[name] = template
        return template


async def create_prompt_manager(temporal_client: Optional[Client] = None) -> PromptManager:
    """Factory function to create a prompt manager."""
    return PromptManager(temporal_client=temporal_client)