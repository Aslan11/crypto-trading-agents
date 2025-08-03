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
        
        # Combine components into final prompt
        return "\n\n".join(comp.content for comp in active_components)
    
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
                    "You are an autonomous portfolio management agent with moderate risk tolerance. "
                    "You operate on scheduled nudges and make data-driven trading decisions for cryptocurrency pairs."
                ),
                priority=1000,
                conditions={"risk_mode": ["moderate"]}
            ),
            
            "role_definition_conservative": PromptComponent(
                name="role_definition_conservative",
                content=(
                    "You are an autonomous portfolio management agent with conservative risk tolerance. "
                    "You prioritize capital preservation and operate on scheduled nudges to make careful, "
                    "data-driven trading decisions for cryptocurrency pairs with strict risk controls."
                ),
                priority=1000,
                conditions={"risk_mode": ["conservative"]}
            ),
            
            "role_definition_aggressive": PromptComponent(
                name="role_definition_aggressive",
                content=(
                    "You are an autonomous portfolio management agent with aggressive risk tolerance. "
                    "You operate on scheduled nudges and make bold, data-driven trading decisions for "
                    "cryptocurrency pairs to maximize returns while managing downside risk."
                ),
                priority=1000,
                conditions={"risk_mode": ["aggressive"]}
            ),
            
            "operational_workflow": PromptComponent(
                name="operational_workflow",
                content=(
                    "OPERATIONAL WORKFLOW:\n"
                    "Each nudge triggers this sequence:\n"
                    "1. Call `get_historical_ticks` once with all symbols and latest processed timestamp\n"
                    "2. Call `get_portfolio_status` once to get current positions and cash\n"
                    "3. Analyze each symbol for trading opportunities\n"
                    "4. Execute safety checks before placing any orders\n"
                    "5. Submit approved orders and generate summary report"
                ),
                priority=900
            ),
            
            "decision_framework": PromptComponent(
                name="decision_framework",
                content=(
                    "DECISION FRAMEWORK:\n"
                    "For each symbol, analyze:\n"
                    "• Price momentum and trend direction from recent ticks\n"
                    "• Volume patterns and market liquidity\n"
                    "• Current position size and portfolio balance\n"
                    "• Risk-reward ratio for potential trades\n"
                    "• Market correlation and portfolio diversification\n\n"
                    "Make one of three decisions: BUY, SELL, or HOLD\n"
                    "Always provide clear rationale for each decision."
                ),
                priority=800,
                conditions={"risk_mode": ["moderate"]}
            ),
            
            "decision_framework_conservative": PromptComponent(
                name="decision_framework_conservative",
                content=(
                    "CONSERVATIVE DECISION FRAMEWORK:\n"
                    "For each symbol, analyze with extra caution:\n"
                    "• Price momentum and trend direction - favor established trends\n"
                    "• Volume patterns and market liquidity - require high liquidity\n"
                    "• Current position size and strict portfolio balance limits\n"
                    "• Risk-reward ratio - minimum 2:1 ratio required\n"
                    "• Market correlation and maximum diversification\n"
                    "• Market sentiment and volatility levels\n\n"
                    "Make one of three decisions: BUY, SELL, or HOLD\n"
                    "Default to HOLD unless strong conviction with low risk.\n"
                    "Always provide detailed rationale for each decision."
                ),
                priority=800,
                conditions={"risk_mode": ["conservative"]}
            ),
            
            "decision_framework_aggressive": PromptComponent(
                name="decision_framework_aggressive",
                content=(
                    "AGGRESSIVE DECISION FRAMEWORK:\n"
                    "For each symbol, analyze for maximum opportunity:\n"
                    "• Price momentum and trend direction - act on emerging trends\n"
                    "• Volume patterns and market liquidity\n"
                    "• Current position size and portfolio balance\n"
                    "• Risk-reward ratio for potential trades - accept 1.5:1 ratios\n"
                    "• Market correlation and strategic concentration\n"
                    "• High-conviction opportunities for outsized returns\n\n"
                    "Make one of three decisions: BUY, SELL, or HOLD\n"
                    "Favor action over inaction when opportunity is present.\n"
                    "Always provide clear rationale for each decision."
                ),
                priority=800,
                conditions={"risk_mode": ["aggressive"]}
            ),
            
            "risk_management": PromptComponent(
                name="risk_management",
                content=(
                    "RISK MANAGEMENT:\n"
                    "Before executing any trade:\n"
                    "• BUY orders: Ensure available cash ≥ (quantity × price × 1.01) for slippage\n"
                    "• SELL orders: Ensure current position ≥ desired sell quantity\n"
                    "• Limit individual position sizes to reasonable portfolio percentages\n"
                    "• If safety checks fail, default to HOLD decision"
                ),
                priority=700,
                conditions={"risk_mode": ["moderate"]}
            ),
            
            "risk_management_conservative": PromptComponent(
                name="risk_management_conservative",
                content=(
                    "RISK MANAGEMENT:\n"
                    "Before executing any trade:\n"
                    "• BUY orders: Ensure available cash ≥ (quantity × price × 1.05) for slippage\n"
                    "• SELL orders: Ensure current position ≥ desired sell quantity\n"
                    "• Limit individual position sizes to maximum 15% of portfolio\n"
                    "• Maintain minimum 20% cash reserves at all times\n"
                    "• If safety checks fail, default to HOLD decision"
                ),
                priority=700,
                conditions={"risk_mode": "conservative"}
            ),
            
            "risk_management_aggressive": PromptComponent(
                name="risk_management_aggressive",
                content=(
                    "RISK MANAGEMENT:\n"
                    "Before executing any trade:\n"
                    "• BUY orders: Ensure available cash ≥ (quantity × price × 1.02) for slippage\n"
                    "• SELL orders: Ensure current position ≥ desired sell quantity\n"
                    "• Allow position sizes up to 25% of portfolio for high-conviction trades\n"
                    "• Maintain minimum 5% cash reserves\n"
                    "• If safety checks fail, default to HOLD decision"
                ),
                priority=700,
                conditions={"risk_mode": "aggressive"}
            ),
            
            "order_execution": PromptComponent(
                name="order_execution",
                content=(
                    "ORDER EXECUTION:\n"
                    "For BUY or SELL decisions, use `place_mock_order` with this exact structure:\n"
                    "{\n"
                    '  "intent": {\n'
                    '    "symbol": <string>,\n'
                    '    "side": "BUY" | "SELL",\n'
                    '    "qty": <number>,\n'
                    '    "price": <number>,\n'
                    '    "type": "market" | "limit"\n'
                    "  }\n"
                    "}\n\n"
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
            )
        }
    
    def _initialize_default_templates(self) -> None:
        """Initialize default prompt templates."""
        # Standard execution agent template with all risk-specific components
        standard_components = [
            self.default_components["role_definition"],
            self.default_components["role_definition_conservative"],
            self.default_components["role_definition_aggressive"],
            self.default_components["operational_workflow"],
            self.default_components["decision_framework"],
            self.default_components["decision_framework_conservative"],
            self.default_components["decision_framework_aggressive"],
            self.default_components["risk_management"],
            self.default_components["risk_management_conservative"],
            self.default_components["risk_management_aggressive"],
            self.default_components["order_execution"],
            self.default_components["reporting"]
        ]
        
        self.templates["execution_agent_standard"] = PromptTemplate(
            name="execution_agent_standard",
            description="Adaptive execution agent prompt that adjusts to user risk tolerance",
            components=standard_components
        )
        
        # Conservative version (with all components available for conditional inclusion)
        conservative_components = [
            self.default_components["role_definition"],
            self.default_components["role_definition_conservative"],
            self.default_components["role_definition_aggressive"],
            self.default_components["operational_workflow"],
            self.default_components["decision_framework"],
            self.default_components["decision_framework_conservative"],
            self.default_components["decision_framework_aggressive"],
            self.default_components["risk_management"],
            self.default_components["risk_management_conservative"],
            self.default_components["risk_management_aggressive"],
            self.default_components["order_execution"],
            self.default_components["reporting"]
        ]
        
        self.templates["execution_agent_conservative"] = PromptTemplate(
            name="execution_agent_conservative",
            description="Conservative execution agent prompt with enhanced risk controls",
            components=conservative_components
        )
        
        # Performance-focused version (with all components available)
        performance_components = [
            self.default_components["role_definition"],
            self.default_components["role_definition_conservative"],
            self.default_components["role_definition_aggressive"],
            self.default_components["operational_workflow"],
            self.default_components["decision_framework"],
            self.default_components["decision_framework_conservative"],
            self.default_components["decision_framework_aggressive"],
            self.default_components["risk_management"],
            self.default_components["risk_management_conservative"],
            self.default_components["risk_management_aggressive"],
            self.default_components["performance_focus"],
            self.default_components["order_execution"],
            self.default_components["reporting"]
        ]
        
        self.templates["execution_agent_performance"] = PromptTemplate(
            name="execution_agent_performance",
            description="Performance-optimized execution agent prompt",
            components=performance_components
        )
    
    async def get_current_prompt(
        self, 
        agent_type: str = "execution_agent",
        context: Optional[Dict] = None
    ) -> str:
        """Get the current prompt for the specified agent type."""
        try:
            if self.temporal_client:
                # Get current prompt version from judge workflow
                handle = self.temporal_client.get_workflow_handle("judge-agent")
                
                # Check if workflow exists first
                try:
                    await handle.describe()
                except Exception as desc_exc:
                    if "not found" in str(desc_exc).lower():
                        logger.info("Judge workflow not yet started, using default template")
                        # Fallback to default template
                        template_name = f"{agent_type}_standard"
                        if template_name in self.templates:
                            return self.templates[template_name].render(context)
                        return self.templates["execution_agent_standard"].render(context)
                    else:
                        raise desc_exc
                
                current_version = await handle.query("get_current_prompt_version")
                
                if current_version and "prompt_content" in current_version:
                    logger.info("Using dynamic prompt from judge workflow (version %s)", 
                               current_version.get("version", "unknown"))
                    return current_version["prompt_content"]
        except Exception as exc:
            logger.warning("Failed to get current prompt from judge workflow: %s", exc)
        
        # Fallback to default template
        logger.info("Using default template for %s", agent_type)
        template_name = f"{agent_type}_standard"
        if template_name in self.templates:
            return self.templates[template_name].render(context)
        
        # Ultimate fallback
        return self.templates["execution_agent_standard"].render(context)
    
    def generate_prompt_variants(
        self, 
        base_template: str,
        performance_data: Dict,
        context: Optional[Dict] = None
    ) -> List[Tuple[str, str, str]]:
        """Generate prompt variants based on performance analysis.
        
        Returns:
            List of (variant_name, description, prompt_content) tuples
        """
        variants = []
        context = context or {}
        
        # Analyze performance to determine needed improvements
        max_drawdown = performance_data.get("max_drawdown", 0.0)
        win_rate = performance_data.get("win_rate", 0.5)
        risk_score = performance_data.get("risk_management_score", 75.0)
        
        # Conservative variant for high drawdown
        if max_drawdown > 0.15:  # More than 15% drawdown
            context["risk_mode"] = "conservative"
            conservative_prompt = self.templates["execution_agent_conservative"].render(context)
            variants.append((
                "conservative_risk",
                "Enhanced risk controls to reduce drawdown",
                conservative_prompt
            ))
        
        # Aggressive variant for overly conservative performance
        if max_drawdown < 0.05 and win_rate < 0.4:  # Low drawdown but poor performance
            context["risk_mode"] = "aggressive"
            aggressive_prompt = self.templates["execution_agent_conservative"].render(context)
            # Modify to be more aggressive
            aggressive_prompt = aggressive_prompt.replace(
                "Limit individual position sizes to maximum 15% of portfolio",
                "Allow position sizes up to 25% of portfolio for high-conviction trades"
            )
            variants.append((
                "increased_risk",
                "Increased position sizing for better returns",
                aggressive_prompt
            ))
        
        # Performance-focused variant for declining performance
        performance_trend = context.get("performance_trend", "stable")
        if performance_trend in ["declining", "poor"]:
            context["performance_trend"] = ["declining", "poor"]
            performance_prompt = self.templates["execution_agent_performance"].render(context)
            variants.append((
                "performance_focus",
                "Enhanced performance monitoring and optimization",
                performance_prompt
            ))
        
        return variants
    
    async def update_agent_prompt(
        self,
        agent_type: str,
        new_prompt: str,
        description: str,
        reason: str,
        changes: List[str]
    ) -> bool:
        """Update the agent's prompt through the judge workflow."""
        try:
            if not self.temporal_client:
                logger.error("No Temporal client available for prompt update")
                return False
            
            # Update prompt version in judge workflow
            prompt_data = {
                "prompt_type": agent_type,
                "prompt_content": new_prompt,
                "description": description,
                "reason": reason,
                "changes": changes
            }
            
            handle = self.temporal_client.get_workflow_handle("judge-agent")
            await handle.signal("update_prompt_version", prompt_data)
            
            logger.info("Updated %s prompt: %s", agent_type, description)
            return True
            
        except Exception as exc:
            logger.error("Failed to update agent prompt: %s", exc)
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