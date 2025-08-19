"""Tests for context management functionality."""

import pytest
from unittest.mock import Mock, AsyncMock
from agents.context_manager import ContextManager, create_context_manager


class TestContextManager:
    def test_token_counting(self):
        """Test token counting functionality."""
        manager = ContextManager()
        
        # Test simple message
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I'm doing well, thank you!"}
        ]
        
        tokens = manager.count_tokens(messages)
        assert tokens > 0
        assert isinstance(tokens, int)
    
    def test_tool_call_token_counting(self):
        """Test token counting with tool calls."""
        manager = ContextManager()
        
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "get_portfolio_status",
                            "arguments": "{}"
                        }
                    }
                ]
            },
            {
                "role": "developer",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "call_1",
                        "output": {"cash": 100000, "positions": {}}
                    }
                ]
            }
        ]
        
        tokens = manager.count_tokens(messages)
        assert tokens > 0
    
    def test_format_messages_for_summary(self):
        """Test message formatting for summarization."""
        manager = ContextManager()
        
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User message"},
            {"role": "assistant", "content": "Assistant response"},
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "test_tool"}}]
            },
            {
                "role": "developer",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_call_id": "test_call",
                        "output": "result"
                    }
                ]
            }
        ]
        
        formatted = manager._format_messages_for_summary(messages)
        
        # Should exclude system message
        assert "System prompt" not in formatted
        # Should include user and assistant messages
        assert "User: User message" in formatted
        assert "Assistant: Assistant response" in formatted
        # Should format tool calls
        assert "Assistant called tool: test_tool" in formatted
        assert "Tool returned data" in formatted
    
    @pytest.mark.asyncio
    async def test_context_management_no_summarization_needed(self):
        """Test context management when no summarization is needed."""
        manager = ContextManager(max_tokens=10000, summary_threshold=8000)
        
        conversation = [
            {"role": "system", "content": "You are a trading agent."},
            {"role": "user", "content": "What's my portfolio status?"},
            {"role": "assistant", "content": "Let me check that for you."}
        ]
        
        result = await manager.manage_context(conversation)
        
        # Should return unchanged since we're under threshold
        assert result == conversation
    
    @pytest.mark.asyncio
    async def test_context_management_with_summarization(self):
        """Test context management with summarization."""
        # Mock OpenAI client
        mock_client = Mock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Summary of previous conversation"
        mock_client.chat.completions.create.return_value = mock_response
        
        manager = ContextManager(
            max_tokens=100,  # Very small to trigger summarization
            summary_threshold=50,
            min_recent_messages=2,
            openai_client=mock_client
        )
        
        # Create a conversation that will exceed token limits
        conversation = [
            {"role": "system", "content": "You are a trading agent."},
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "First response"},
            {"role": "user", "content": "Second message"},
            {"role": "assistant", "content": "Second response"},
            {"role": "user", "content": "Third message"},
            {"role": "assistant", "content": "Third response"},
            {"role": "user", "content": "Recent message"},
            {"role": "assistant", "content": "Recent response"}
        ]
        
        result = await manager.manage_context(conversation)
        
        # Should have system message, summary, and recent messages
        assert len(result) >= 4  # system + summary + at least 2 recent
        assert result[0]["role"] == "system"
        assert "CONVERSATION SUMMARY" in result[1]["content"]
        
        # Should preserve recent messages
        assert result[-2]["content"] == "Recent message"
        assert result[-1]["content"] == "Recent response"
    
    @pytest.mark.asyncio
    async def test_summarization_fallback(self):
        """Test summarization fallback when OpenAI client is unavailable."""
        manager = ContextManager(openai_client=None)
        
        messages = [
            {"role": "user", "content": "Test message"},
            {"role": "assistant", "content": "Test response"}
        ]
        
        summary = await manager.summarize_messages(messages)
        
        assert "[SUMMARY]" in summary
        assert "Previous conversation" in summary


def test_create_context_manager():
    """Test the factory function for creating context managers."""
    manager = create_context_manager(model="gpt-4o")
    
    assert isinstance(manager, ContextManager)
    assert manager.model == "gpt-4o"
    assert manager.max_tokens > 0
    assert manager.summary_threshold > 0
    assert manager.min_recent_messages == 5


def test_create_context_manager_with_unknown_model():
    """Test context manager creation with unknown model."""
    manager = create_context_manager(model="unknown-model")
    
    assert isinstance(manager, ContextManager)
    assert manager.model == "unknown-model"
    # Should use default token limits
    assert manager.max_tokens == int(8000 * 0.7)  # Default model limit


if __name__ == "__main__":
    pytest.main([__file__])