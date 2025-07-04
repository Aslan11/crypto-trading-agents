"""Utility helpers shared across agents."""

from __future__ import annotations


def print_banner(name: str, purpose: str) -> None:
    """Print a simple ASCII banner with ``name`` and ``purpose``."""
    lines = [name, purpose]
    width = max(len(line) for line in lines) + 4
    border = "*" * width
    print(border)
    for line in lines:
        print(f"* {line.ljust(width - 4)} *")
    print(border)

from pprint import pformat
from typing import Any


def format_log(data: Any) -> str:
    """Return a pretty string representation of ``data`` for logging."""
    if isinstance(data, str):
        return data
    return pformat(data, width=60)





def stream_chat_completion(client, *, prefix: str = "", color: str = "", reset: str = "", **kwargs) -> dict:
    """Stream chat completion tokens to stdout and return the final message.

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
        Additional parameters forwarded to ``client.chat.completions.create``.
    """

    stream = client.chat.completions.create(stream=True, **kwargs)
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    first_token = True

    for chunk in stream:
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        if not delta:
            continue
        token = getattr(delta, "content", None)
        if token:
            if first_token:
                if prefix or color:
                    print(f"{color}{prefix}", end="", flush=True)
                first_token = False
            print(token, end="", flush=True)
            content_parts.append(token)
        for tc in getattr(delta, "tool_calls", []) or []:
            entry = tool_calls.setdefault(
                getattr(tc, "index", 0),
                {
                    "id": tc.id,
                    "type": getattr(tc, "type", "function"),
                    "function": {"name": "", "arguments": ""},
                },
            )
            name = getattr(tc.function, "name", "")
            if name:
                entry["function"]["name"] += name
            args = getattr(tc.function, "arguments", "")
            if args:
                entry["function"]["arguments"] += args

    if not first_token and reset:
        print(reset)
    else:
        print()
    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return message


__all__ = ["print_banner", "format_log", "stream_chat_completion"]

