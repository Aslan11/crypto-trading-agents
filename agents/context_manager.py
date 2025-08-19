"""Intelligent context management for agent conversations."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List
import tiktoken
import openai

logger = logging.getLogger(__name__)

class ContextManager:
    """Manages conversation context with token-based sliding windows and summarization."""
    
    def __init__(
        self,
        model: str = "gpt-4o",
        max_tokens: int = 8000,
        summary_threshold: int = 6000,
        min_recent_messages: int = 5,
        openai_client: openai.OpenAI | None = None
    ):
        """Initialize context manager.
        
        Args:
            model: Model name for token counting and summarization
            max_tokens: Maximum tokens to maintain in conversation
            summary_threshold: Token count that triggers summarization
            min_recent_messages: Minimum recent messages to always keep
            openai_client: OpenAI client for summarization
        """
        self.model = model
        self.max_tokens = max_tokens
        self.summary_threshold = summary_threshold
        self.min_recent_messages = min_recent_messages
        self.openai_client = openai_client
        
        # Initialize tokenizer
        try:
            self.encoding = tiktoken.encoding_for_model(model)
        except Exception:
            try:
                self.encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:
                self.encoding = None
    
    def count_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Count tokens in a conversation."""
        total_tokens = 0
        for message in messages:
            # Add tokens for role
            if self.encoding:
                total_tokens += len(self.encoding.encode(message.get("role", "")))
            else:
                total_tokens += len(message.get("role", ""))
            
            # Add tokens for content
            content = message.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        output = part.get("output", "")
                        if self.encoding:
                            total_tokens += len(self.encoding.encode(str(output)))
                            total_tokens += len(
                                self.encoding.encode(str(part.get("tool_call_id", "")))
                            )
                        else:
                            total_tokens += len(str(output))
                            total_tokens += len(str(part.get("tool_call_id", "")))
            elif content:
                if self.encoding:
                    total_tokens += len(self.encoding.encode(str(content)))
                else:
                    total_tokens += len(str(content))
            
            # Add tokens for tool calls
            if "tool_calls" in message:
                for tool_call in message["tool_calls"]:
                    func_data = tool_call.get("function", {})
                    if self.encoding:
                        total_tokens += len(self.encoding.encode(func_data.get("name", "")))
                        total_tokens += len(self.encoding.encode(func_data.get("arguments", "")))
                    else:
                        total_tokens += len(func_data.get("name", ""))
                        total_tokens += len(func_data.get("arguments", ""))
            
            # Add tokens for function calls (legacy format)
            if "function_call" in message:
                func_call = message["function_call"]
                if self.encoding:
                    total_tokens += len(self.encoding.encode(func_call.get("name", "")))
                    total_tokens += len(self.encoding.encode(func_call.get("arguments", "")))
                else:
                    total_tokens += len(func_call.get("name", ""))
                    total_tokens += len(func_call.get("arguments", ""))
            
            # Add overhead per message (role markers, formatting)
            total_tokens += 4
        
        return total_tokens
    
    async def summarize_messages(self, messages: List[Dict[str, Any]]) -> str:
        """Summarize a list of messages into a concise summary."""
        if not self.openai_client:
            # Fallback: simple text truncation
            combined = " ".join([
                f"{msg.get('role', 'unknown')}: {str(msg.get('content', ''))[:100]}" 
                for msg in messages
            ])
            return f"[SUMMARY] Previous conversation: {combined[:500]}..."
        
        # Prepare messages for summarization
        conversation_text = self._format_messages_for_summary(messages)
        
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",  # Use smaller model for summarization
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a conversation summarizer for crypto trading agents. "
                            "Summarize the key points, decisions, and context from the conversation below. "
                            "Focus on trading decisions, market data, portfolio changes, and important insights. "
                            "Keep the summary concise but preserve critical trading context."
                        )
                    },
                    {
                        "role": "user", 
                        "content": f"Summarize this conversation:\n\n{conversation_text}"
                    }
                ],
                max_tokens=300,
                temperature=0.1
            )
            summary = response.choices[0].message.content
            return f"[CONVERSATION SUMMARY] {summary}"
        except Exception as exc:
            logger.error("Failed to generate summary: %s", exc)
            # Fallback to simple truncation
            return f"[SUMMARY] Previous conversation included {len(messages)} messages about trading decisions and market analysis."
    
    def _format_messages_for_summary(self, messages: List[Dict[str, Any]]) -> str:
        """Format messages into readable text for summarization."""
        formatted = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            
            if role == "system":
                continue  # Skip system messages in summary
            elif role == "assistant" and "tool_calls" in msg:
                # Format tool calls
                for tool_call in msg["tool_calls"]:
                    func_name = tool_call.get("function", {}).get("name", "")
                    formatted.append(f"Assistant called tool: {func_name}")
            elif role == "tool":
                tool_name = msg.get("name", "unknown_tool")
                formatted.append(f"Tool {tool_name} returned data")
            elif role == "developer":
                parts = msg.get("content") or []
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        formatted.append("Tool returned data")
            else:
                # Regular message content
                if content:
                    formatted.append(f"{role.title()}: {str(content)[:200]}")
        
        return "\n".join(formatted)
    
    async def manage_context(
        self, 
        conversation: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Manage conversation context with token-based sliding window and summarization."""
        if not conversation:
            return conversation
        
        # Always preserve system message
        system_msg = conversation[0] if conversation[0].get("role") == "system" else None
        messages = conversation[1:] if system_msg else conversation
        
        current_tokens = self.count_tokens(conversation)
        
        # If we're under the threshold, return as-is
        if current_tokens <= self.summary_threshold:
            return conversation
        
        # Ensure we keep minimum recent messages
        recent_messages = messages[-self.min_recent_messages:] if len(messages) > self.min_recent_messages else messages
        recent_tokens = self.count_tokens(recent_messages)
        
        # Calculate how many tokens we have available for older messages
        available_tokens = self.max_tokens - recent_tokens
        if system_msg:
            available_tokens -= self.count_tokens([system_msg])
        
        # Find the split point for summarization
        messages_to_summarize = []
        messages_to_keep = []
        
        if len(messages) > self.min_recent_messages:
            older_messages = messages[:-self.min_recent_messages]
            
            # Binary search to find optimal split point
            left, right = 0, len(older_messages)
            best_split = 0
            
            while left <= right:
                mid = (left + right) // 2
                keep_msgs = older_messages[mid:]
                keep_tokens = self.count_tokens(keep_msgs)
                
                if keep_tokens <= available_tokens:
                    best_split = mid
                    right = mid - 1
                else:
                    left = mid + 1
            
            messages_to_summarize = older_messages[:best_split]
            messages_to_keep = older_messages[best_split:]
        
        # Create the new conversation
        result = []
        
        # Add system message
        if system_msg:
            result.append(system_msg)
        
        # Add summary if we have messages to summarize
        if messages_to_summarize:
            summary = await self.summarize_messages(messages_to_summarize)
            result.append({"role": "assistant", "content": summary})
        
        # Add kept older messages
        result.extend(messages_to_keep)
        
        # Add recent messages
        result.extend(recent_messages)
        
        final_tokens = self.count_tokens(result)
        logger.info(
            "Context managed: %d -> %d tokens, summarized %d messages", 
            current_tokens, final_tokens, len(messages_to_summarize)
        )
        
        return result


def create_context_manager(
    model: str = "gpt-4o",
    openai_client: openai.OpenAI | None = None
) -> ContextManager:
    """Factory function to create a context manager with sensible defaults."""
    # Model-specific token limits (conservative estimates)
    model_limits = {
        "gpt-4o": 120000,
        "gpt-4o-mini": 120000,
        "gpt-5-mini": 400000,
        "gpt-4": 8000,
        "gpt-3.5-turbo": 4000,
    }
    
    max_context = model_limits.get(model, 8000)
    # Use 70% of context for conversation, reserve 30% for response
    max_tokens = int(max_context * 0.7)
    summary_threshold = int(max_tokens * 0.75)
    
    return ContextManager(
        model=model,
        max_tokens=max_tokens,
        summary_threshold=summary_threshold,
        min_recent_messages=5,
        openai_client=openai_client
    )