"""
In-process event bus for Service A.

Replaces Google Cloud Pub/Sub. After a shipment is successfully extracted
and saved to SQLite, Service A publishes a message here. Downstream services
(Service B, C, D) are async consumers of this queue.

In a single-process FastAPI app, all services share this module-level queue.
If you later split into separate processes, swap asyncio.Queue for Redis pub/sub
by changing only this file.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Module-level queue — one instance shared across the entire application lifetime
_queue: asyncio.Queue = asyncio.Queue()


async def publish(event_type: str, payload: dict[str, Any]) -> None:
    """
    Publish an event to the internal bus.

    Args:
        event_type: String identifier for the event e.g. "shipment.created"
        payload: Dictionary of event data. Must be JSON-serialisable.
    """
    message = {"event_type": event_type, "payload": payload}
    await _queue.put(message)
    logger.debug("Event published: %s | shipment_id=%s", event_type, payload.get("shipment_id"))


async def consume() -> dict[str, Any]:
    """
    Consume the next event from the bus (blocks until one is available).
    Intended to be called inside a long-running async background task.
    """
    return await _queue.get()


def task_done() -> None:
    """Mark the last consumed item as processed. Call after handling each event."""
    _queue.task_done()


def queue_size() -> int:
    """Return the number of unprocessed events currently in the queue."""
    return _queue.qsize()
