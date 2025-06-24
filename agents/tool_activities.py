from __future__ import annotations

import logging
import os
from typing import Any


class ToolActivities:
    """Minimal tool activity helper for LLM prompts."""

    def __init__(self, openai_module: Any | None = None) -> None:
        self.openai = openai_module
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        logging.getLogger(__name__).info(
            "ToolActivities using model %s", self.model
        )

    async def agent_toolPlanner(self, prompt: str) -> str:
        if self.openai is None:
            raise RuntimeError("openai module not available")
        client = self.openai.AsyncOpenAI()
        resp = await client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
