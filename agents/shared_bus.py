import asyncio

APPROVED_INTENT_QUEUE: asyncio.Queue[dict] = asyncio.Queue()


def enqueue_intent(intent: dict) -> None:
    """Enqueue an approved order intent for execution."""
    APPROVED_INTENT_QUEUE.put_nowait(intent)

