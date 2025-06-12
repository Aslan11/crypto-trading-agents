"""Strategy signal logging and evaluation tools."""

from __future__ import annotations

import logging
from datetime import timedelta

from temporalio import activity, workflow


logger = logging.getLogger(__name__)


@activity.defn
def log_signal(signal: dict) -> dict:
    """Log and return the provided signal payload."""
    logger.info("Momentum signal received: %s", signal)
    return signal


@workflow.defn
class EvaluateStrategyMomentum:
    """Workflow wrapper around :func:`log_signal` with an optional cooldown."""

    @workflow.run
    async def run(self, signal: dict, cooldown_sec: int | None = None) -> dict:
        """Execute ``log_signal`` then sleep ``cooldown_sec`` seconds if set."""
        await workflow.execute_activity(
            log_signal,
            signal,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        if cooldown_sec:
            await workflow.sleep(cooldown_sec)
        return signal
