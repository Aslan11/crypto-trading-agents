from __future__ import annotations

from datetime import timedelta
from typing import Dict, List

from temporalio import workflow


@workflow.defn
class FetchHistoricalTicks:
    """Gather tick history from existing feature workflows."""

    @workflow.run
    async def run(
        self, symbols: List[str], days: int | None = None
    ) -> Dict[str, List[Dict[str, float]]]:
        cutoff = 0
        if days is not None:
            now = workflow.now()
            cutoff = int((now - timedelta(days=days)).timestamp())

        results: Dict[str, List[Dict[str, float]]] = {}
        for symbol in symbols:
            wf_id = f"feature-{symbol.replace('/', '-')}"
            handle = workflow.get_external_workflow_handle(wf_id)
            try:
                ticks = await handle.query("historical_ticks", cutoff)
            except Exception:
                ticks = []
            results[symbol] = [
                {"ts": int(t["ts"]), "price": float(t["price"])} for t in ticks
            ]
        return results
