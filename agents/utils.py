"""Utility helpers shared across agents."""

from __future__ import annotations

from pprint import pformat
from typing import Any


def print_banner(name: str, purpose: str) -> None:
    """Print a simple ASCII banner with ``name`` and ``purpose``."""
    lines = [name, purpose]
    width = max(len(line) for line in lines) + 4
    border = "*" * width
    print(border)
    for line in lines:
        print(f"* {line.ljust(width - 4)} *")
    print(border)


def format_log(data: Any) -> str:
    """Return a pretty string representation of ``data`` for logging."""
    if isinstance(data, str):
        return data
    return pformat(data, width=60)





def stream_response(client, *, prefix: str = "", color: str = "", reset: str = "", **kwargs) -> dict:
    """Stream model responses to stdout and return the final message.

    Parameters
    ----------
    client:
        The OpenAI client to use.
    prefix:
        Optional text printed once before the first token.
    color:
        ANSI escape code used for coloring the streamed text.
    reset:
        ANSI escape code used to reset the terminal color when streaming ends.
    kwargs:
        Additional parameters forwarded to ``client.responses.stream``.
    """

    # Support legacy ``messages`` and ``reasoning_effort`` parameters
    if "messages" in kwargs and "input" not in kwargs:
        messages = kwargs.pop("messages")
        converted = []
        for msg in messages:
            role = msg.get("role")
            new_msg = {k: v for k, v in msg.items() if k != "content"}
            content = msg.get("content")
            if isinstance(content, list):
                new_content = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part_type = "output_text" if role == "assistant" else "input_text"
                        part = {**part, "type": part_type}
                    new_content.append(part)
                new_msg["content"] = new_content
            elif content is None:
                new_msg["content"] = []
            else:
                part_type = "output_text" if role == "assistant" else "input_text"
                new_msg["content"] = [{"type": part_type, "text": str(content)}]
            converted.append(new_msg)
        kwargs["input"] = converted
    if "reasoning_effort" in kwargs and "reasoning" not in kwargs:
        kwargs["reasoning"] = {"effort": kwargs.pop("reasoning_effort")}

    # Convert legacy Chat Completions tool format to Responses API shape
    if "tools" in kwargs:
        converted_tools: list[dict] = []
        for tool in kwargs["tools"]:
            if isinstance(tool, dict) and tool.get("type") == "function":
                # Chat Completions style uses nested "function" object
                if "function" in tool:
                    fn = dict(tool["function"])
                    schema = fn.get("input_schema") or fn.get("parameters") or {}
                    converted_tools.append(
                        {
                            "type": "function",
                            "name": fn.get("name"),
                            "description": fn.get("description"),
                            "parameters": schema,
                            "strict": fn.get("strict", True),
                        }
                    )
                else:
                    # Already Responses API style but may use "input_schema"
                    if "input_schema" in tool and "parameters" not in tool:
                        tool = {**tool, "parameters": tool.pop("input_schema")}
                    if "strict" not in tool:
                        tool = {**tool, "strict": True}
                    converted_tools.append(tool)
            else:
                converted_tools.append(tool)
        kwargs["tools"] = converted_tools

    content_parts: list[str] = []
    tool_calls: list[dict] = []
    first_token = True

    with client.responses.stream(**kwargs) as stream:
        for event in stream:
            if event.type == "response.output_text.delta":
                token = event.delta
                if first_token:
                    if prefix or color:
                        print(f"{color}{prefix}", end="", flush=True)
                    first_token = False
                print(token, end="", flush=True)
                content_parts.append(token)

        final = stream.get_final_response()

    if not first_token and reset:
        print(reset)
    else:
        print()

    for item in getattr(final, "output", []) or []:
        if getattr(item, "type", "") == "function_call":
            tool_calls.append(
                {
                    "id": getattr(item, "id", None) or item.call_id,
                    "type": "function",
                    "function": {"name": item.name, "arguments": item.arguments},
                }
            )

    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


__all__ = ["print_banner", "format_log", "stream_response"]

