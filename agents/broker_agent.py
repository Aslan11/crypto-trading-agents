from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import List

import aiohttp

try:
    import openai
except Exception:  # pragma: no cover - optional dependency
    openai = None


MCP_HOST = os.environ.get("MCP_HOST", "localhost")
MCP_PORT = os.environ.get("MCP_PORT", "8080")
EXCHANGE = os.environ.get("EXCHANGE", "coinbaseexchange")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _parse_symbols(text: str) -> List[str]:
    """Return list of crypto symbols in ``text``."""
    return re.findall(r"[A-Z0-9]+/[A-Z0-9]+", text.upper())


async def _prompt_user() -> List[str]:
    """Interact with the user via an LLM to gather crypto symbols."""
    if openai is None:
        print("openai package not installed")
        return []

    client = openai.AsyncOpenAI()
    messages = [
        {
            "role": "system",
            "content": (
                "You are a crypto trading broker. "
                "Only discuss cryptocurrency trading and decline other topics."
            ),
        }
    ]

    prompt = "Which cryptocurrency pairs would you like to trade? e.g. BTC/USD, ETH/USD"
    print(prompt)
    user_msg = input("> ").strip()
    messages.append({"role": "user", "content": user_msg})

    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL, messages=messages
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as exc:  # pragma: no cover - network issues
        logger.error("LLM call failed: %s", exc)
        reply = ""
    print(reply)

    symbols = _parse_symbols(user_msg) or _parse_symbols(reply)
    if not symbols:
        print("No valid crypto symbols found. Exiting.")
    return symbols


async def _start_stream(symbols: List[str]) -> None:
    payload = {"exchange": EXCHANGE, "symbols": symbols}
    url = f"http://{MCP_HOST}:{MCP_PORT}/tools/SubscribeCEXStream"
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 202:
                    data = await resp.json()
                    print(
                        f"Started workflow {data['workflow_id']} run {data['run_id']}"
                    )
                else:
                    print(f"Failed to start workflow: {resp.status}")
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("HTTP request failed: %s", exc)


async def main() -> None:
    symbols = await _prompt_user()
    if not symbols:
        return
    await _start_stream(symbols)


if __name__ == "__main__":
    asyncio.run(main())
