"""
Service B — Telemetry Ingestion API
Pub/Sub publisher helper.

Publishes a validated TelemetryPayload to the telemetry-stream topic.
The PublisherClient is initialised at module level so it is reused across
warm Cloud Function instances — avoids reconnection overhead per invocation.

Message format:
  - JSON-encoded TelemetryPayload dict
  - UTF-8 encoded bytes (Pub/Sub handles base64 encoding internally)
  - Attributes carry drug_id and source for routing/filtering

Service C decodes messages as:
  raw_data = base64.b64decode(envelope.message.data).decode("utf-8")
  telemetry_dict = json.loads(raw_data)
"""

import json
import logging
import os

from google.cloud import pubsub_v1

logger = logging.getLogger(__name__)

PROJECT_ID    = os.environ["GOOGLE_CLOUD_PROJECT"]
TOPIC_NAME    = os.environ.get("TELEMETRY_TOPIC", "telemetry-stream")

# Module-level client — reused across warm instances
_publisher  = pubsub_v1.PublisherClient()
_topic_path = _publisher.topic_path(PROJECT_ID, TOPIC_NAME)


def publish_telemetry(payload: dict) -> str:
    """
    Publish a validated telemetry payload dict to the telemetry-stream topic.

    Blocks until the publish is confirmed (.result()) before returning so the
    HTTP caller receives a success response only after the message is durably
    handed off to Pub/Sub.

    Args:
        payload: Dict representation of a validated TelemetryPayload.

    Returns:
        The Pub/Sub message ID string.

    Raises:
        Exception: Propagates any Pub/Sub publish failure to the caller.
    """
    message_bytes = json.dumps(payload).encode("utf-8")

    future = _publisher.publish(
        _topic_path,
        data=message_bytes,
        drug_id=payload["drug_id"],
        source="service_b",
    )

    # Block until confirmed — do not return HTTP 200 before this resolves
    message_id = future.result(timeout=10)

    logger.info(
        "Published to %s | message_id=%s | drug_id=%s",
        TOPIC_NAME, message_id, payload["drug_id"],
    )
    return message_id
