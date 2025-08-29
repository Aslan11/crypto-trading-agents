"""Feature engineering tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from temporalio import activity, workflow
from temporalio.client import Client
import os
import asyncio
import logging
from agents.constants import VECTOR_WINDOW_SEC, VECTOR_CONTINUE_EVERY, VECTOR_HISTORY_LIMIT

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "mcp-tools")

_TEMPORAL_CLIENT: Client | None = None


async def _get_client() -> Client:
    """Return a cached Temporal client."""
    global _TEMPORAL_CLIENT
    if _TEMPORAL_CLIENT is None:
        _TEMPORAL_CLIENT = await Client.connect(
            TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE
        )
    return _TEMPORAL_CLIENT


@activity.defn
async def signal_compute_vector(symbol: str, tick: dict) -> None:
    """Start or signal a ComputeFeatureVector workflow for ``symbol``."""
    client = await _get_client()
    wf_id = f"feature-{symbol.replace('/', '-')}"
    await client.start_workflow(
        ComputeFeatureVector.run,
        id=wf_id,
        task_queue=TASK_QUEUE,
        start_signal="market_tick",
        start_signal_args=[tick],
        args=[symbol, VECTOR_WINDOW_SEC, VECTOR_CONTINUE_EVERY, VECTOR_HISTORY_LIMIT],
    )


@activity.defn
async def load_historical_data(symbol: str, historical_ticks: List[dict]) -> None:
    """Load historical ticks into the feature workflow.
    
    Parameters
    ----------
    symbol:
        Trading pair (e.g., 'BTC/USD')
    historical_ticks:
        List of historical tick data to preload
    """
    if not historical_ticks:
        logger.info("No historical data to load for %s", symbol)
        return
        
    client = await _get_client()
    wf_id = f"feature-{symbol.replace('/', '-')}"
    
    try:
        handle = client.get_workflow_handle(wf_id)
        
        # Send historical ticks as signals to populate the workflow's history
        batch_size = 100  # Send in batches to avoid overwhelming the workflow
        for i in range(0, len(historical_ticks), batch_size):
            batch = historical_ticks[i:i + batch_size]
            for tick in batch:
                # Create tick in the format expected by the workflow
                market_tick = {
                    "exchange": "coinbaseexchange",
                    "symbol": symbol,
                    "data": tick
                }
                await handle.signal("market_tick", market_tick)
            
            # Small delay between batches
            if i + batch_size < len(historical_ticks):
                await asyncio.sleep(0.1)
        
        logger.info("Loaded %d historical ticks for %s into feature workflow", 
                   len(historical_ticks), symbol)
                   
    except Exception as exc:
        logger.error("Failed to load historical data for %s: %s", symbol, exc)


@workflow.defn
class ComputeFeatureVector:
    """Continuously compute feature vectors from ``market_tick`` signals."""

    def __init__(self) -> None:
        self.symbol = ""
        self.window_sec = VECTOR_WINDOW_SEC
        self._ticks: List[dict] = []
        # Store the full tick history for queries
        self._history: List[dict] = []
        # Use an asyncio.Event for signalling between the signal handler and
        # the main workflow loop. Temporal workflows should avoid waiting on
        # events directly, so we will wait via ``workflow.wait_condition``.
        self._event = asyncio.Event()

    @workflow.query
    def historical_ticks(self, since_ts: int = 0) -> list[dict]:
        """Return stored ticks newer than ``since_ts`` sorted oldest to newest.

        Parameters
        ----------
        since_ts:
            Unix timestamp in seconds. Only ticks at or after this time are
            returned.
        """

        ticks: list[dict] = []
        for t in self._history:
            ts_ms = t.get("timestamp")
            if ts_ms is None:
                continue
            ts = int(ts_ms / 1000)
            if ts < since_ts:
                continue
            if "last" in t:
                price = float(t["last"])
            elif {"bid", "ask"}.issubset(t):
                price = (float(t["bid"]) + float(t["ask"])) / 2
            else:
                continue
            ticks.append({"ts": ts, "price": price})
        ticks = sorted(ticks, key=lambda x: x["ts"])
        logger.info("historical_ticks returning %d items for %s", len(ticks), self.symbol)
        return ticks

    @workflow.query
    def get_latest_price(self) -> dict:
        """Return the most recent price with timestamp and age information.
        
        Returns
        -------
        dict
            Dictionary with keys:
            - price: float or None if no valid price available
            - timestamp: int Unix timestamp in seconds or None
            - age_seconds: float seconds since price was recorded, inf if no price
        """
        if not self._history:
            return {"price": None, "timestamp": None, "age_seconds": float('inf')}
        
        # Get most recent tick by timestamp
        latest_tick = max(self._history, key=lambda t: t.get("timestamp", 0))
        
        # Extract price using same logic as historical_ticks
        if "last" in latest_tick:
            price = float(latest_tick["last"])
        elif {"bid", "ask"}.issubset(latest_tick):
            price = (float(latest_tick["bid"]) + float(latest_tick["ask"])) / 2
        else:
            return {"price": None, "timestamp": None, "age_seconds": float('inf')}
        
        ts_ms = latest_tick.get("timestamp")
        if ts_ms is None:
            return {"price": None, "timestamp": None, "age_seconds": float('inf')}
        
        ts = int(ts_ms / 1000)
        current_time = int(datetime.now(timezone.utc).timestamp())
        age_seconds = current_time - ts
        
        return {
            "price": price,
            "timestamp": ts,
            "age_seconds": age_seconds
        }

    @workflow.signal
    def market_tick(self, tick: dict) -> None:
        if tick.get("symbol") != self.symbol:
            return
        data = tick.get("data", {})
        self._ticks.append(data)
        self._history.append(data)
        logger.debug("Received tick for %s: %s", self.symbol, data)
        self._event.set()

    @workflow.run
    async def run(
        self,
        symbol: str,
        window_sec: int = VECTOR_WINDOW_SEC,
        continue_every: int = VECTOR_CONTINUE_EVERY,
        history_limit: int = VECTOR_HISTORY_LIMIT,
        history: list[dict] | None = None,
    ) -> None:
        """Process ticks and emit feature vectors indefinitely.

        Parameters
        ----------
        symbol:
            Trading pair in ``BASE/QUOTE`` format.
        window_sec:
            Sliding window size for feature calculation.
        continue_every:
            Number of cycles before continuing as new.
        history_limit:
            Maximum workflow history events before continuing as new.
        history:
            Previously stored ticks carried over from a prior run.
        """

        self.symbol = symbol
        self.window_sec = window_sec
        logger.info(
            "ComputeFeatureVector starting for %s window=%s", symbol, window_sec
        )
        
        # Load historical data if this is a fresh start (no history from continue-as-new)
        if history is None:
            from tools.market_data import fetch_historical_ohlcv, HISTORICAL_MINUTES
            try:
                historical_data = await workflow.execute_activity(
                    fetch_historical_ohlcv,
                    args=[symbol, '1m', HISTORICAL_MINUTES],
                    schedule_to_close_timeout=timedelta(seconds=60),
                )
                if historical_data:
                    self._history = list(historical_data)
                    logger.info("Loaded %d historical ticks for %s on workflow startup", 
                               len(historical_data), symbol)
            except Exception as exc:
                logger.warning("Failed to load historical data for %s on startup: %s", symbol, exc)
                self._history = []
        else:
            self._history = list(history)
        cycles = 0
        while True:
            # Wait for a new tick to be signalled via the event. We use
            # ``workflow.wait_condition`` so that the wait is deterministic in
            # Temporal's workflow environment.
            await workflow.wait_condition(lambda: self._event.is_set())
            self._event.clear()

            now = workflow.now()
            since = now - timedelta(seconds=self.window_sec)
            pruned: List[dict] = []
            for t in self._ticks:
                ts_ms = t.get("timestamp")
                if ts_ms is None:
                    continue
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                if ts >= since:
                    pruned.append(t)
            self._ticks = pruned

            if not self._ticks:
                # Wait for a new tick if all have expired
                continue
            cycles += 1
            hist_len = workflow.info().get_current_history_length()
            if (
                hist_len >= history_limit
                or workflow.info().is_continue_as_new_suggested()
            ):
                await workflow.continue_as_new(
                    args=[
                        symbol,
                        window_sec,
                        continue_every,
                        history_limit,
                        self._history,
                    ]
                )
            if cycles >= continue_every:
                await workflow.continue_as_new(
                    args=[
                        symbol,
                        window_sec,
                        continue_every,
                        history_limit,
                        self._history,
                    ]
                )
