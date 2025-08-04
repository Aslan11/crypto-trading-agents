"""Tests for the context manager sliding window implementation."""

import pytest
from unittest.mock import Mock, AsyncMock
from agents.context_manager import ContextManager, create_context_manager


class TestContextManager:
    """Test cases for ContextManager."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.context_manager = ContextManager(
            model="gpt-4o-mini",
            max_tokens=1000,
            summary_threshold=800,
            min_recent_messages=3,
            openai_client=self.mock_client
        )
    
    def test_token_counting(self):
        """Test token counting for different message types."""
        messages = [
            {"role": "system", "content": "You are a trading agent."},
            {"role": "user", "content": "What is the current price?"},
            {"role": "assistant", "content": "The price is $45,000"},
            {
                "role": "assistant", 
                "content": "I'll check the portfolio",
                "tool_calls": [{"function": {"name": "get_portfolio", "arguments": "{}"}}]
            },
            {"role": "tool", "name": "get_portfolio", "content": '{"cash": 1000}'}
        ]
        
        token_count = self.context_manager.count_tokens(messages)
        assert token_count > 0
        assert isinstance(token_count, int)
    
    def test_format_messages_for_summary(self):
        """Test message formatting for summarization."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Buy BTC"},
            {"role": "assistant", "content": "Placing buy order for BTC"},
            {"role": "tool", "name": "place_order", "content": '{"status": "filled"}'}
        ]
        
        formatted = self.context_manager._format_messages_for_summary(messages)
        
        # System message should be skipped
        assert "System prompt" not in formatted
        # Other messages should be included
        assert "User: Buy BTC" in formatted
        assert "Tool place_order returned data" in formatted
    
    @pytest.mark.asyncio
    async def test_manage_context_no_action_needed(self):
        """Test context management when no action is needed."""
        # Short conversation under threshold
        conversation = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"}
        ]
        
        result = await self.context_manager.manage_context(conversation)
        
        # Should return unchanged since under threshold
        assert result == conversation
    
    @pytest.mark.asyncio
    async def test_manage_context_with_summarization(self):
        """Test context management with summarization."""
        # Create a long conversation that exceeds threshold
        conversation = [{"role": "system", "content": "You are a trading agent."}]
        
        # Add many messages to exceed token threshold
        for i in range(50):
            conversation.extend([
                {"role": "user", "content": f"What about symbol {i}? I need detailed analysis and recommendations for this trading pair including technical indicators, volume analysis, market sentiment, and risk assessment."},
                {"role": "assistant", "content": f"Symbol {i} is at price ${i * 100}. Based on technical analysis, this symbol shows strong momentum with RSI at 65, MACD showing bullish crossover, and volume increasing by 25% over the past 24 hours."},
                {"role": "tool", "name": "get_price", "content": f'{{"price": {i * 100}, "volume": {i * 1000}, "rsi": 65, "macd": "bullish", "support": {i * 90}, "resistance": {i * 110}}}'}
            ])
        
        # Mock the summarization response
        mock_choice = Mock()
        mock_choice.message.content = "Summary of trading decisions"
        mock_response = Mock()
        mock_response.choices = [mock_choice]
        self.mock_client.chat.completions.create.return_value = mock_response
        
        result = await self.context_manager.manage_context(conversation)
        
        # Should be shorter than original
        assert len(result) < len(conversation)
        # Should preserve system message
        assert result[0]["role"] == "system"
        # Should have a summary message
        summary_found = any("[CONVERSATION SUMMARY]" in msg.get("content", "") for msg in result)
        assert summary_found
    
    @pytest.mark.asyncio
    async def test_summarization_fallback(self):
        """Test fallback when OpenAI client is unavailable."""
        context_manager = ContextManager(
            model="gpt-4o",
            max_tokens=1000,
            summary_threshold=800,
            min_recent_messages=3,
            openai_client=None  # No client
        )
        
        messages = [
            {"role": "user", "content": "Test message"},
            {"role": "assistant", "content": "Test response"}
        ]
        
        summary = await context_manager.summarize_messages(messages)
        
        # Should provide fallback summary
        assert "[SUMMARY]" in summary
        assert "Previous conversation" in summary


def test_create_context_manager_factory():
    """Test the factory function for creating context managers."""
    mock_client = Mock()
    
    # Test with gpt-4o (128k context)
    manager = create_context_manager("gpt-4o", mock_client)
    assert manager.model == "gpt-4o"
    assert manager.max_tokens == int(128000 * 0.5)  # 50% of context for large models
    assert manager.openai_client == mock_client
    
    # Test with o4-mini (2M context - very large)
    manager = create_context_manager("o4-mini", mock_client)
    assert manager.model == "o4-mini"
    assert manager.max_tokens == int(2000000 * 0.1)  # 10% for very large models
    assert manager.min_recent_messages == 10  # More recent messages for large models
    
    # Test with unknown model (should use default)
    manager = create_context_manager("unknown-model", mock_client)
    assert manager.model == "unknown-model"
    assert manager.max_tokens == int(8192 * 0.7)  # Default fallback


if __name__ == "__main__":
    pytest.main([__file__])