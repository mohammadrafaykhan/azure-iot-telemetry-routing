"""Device-to-cloud telemetry contract.

Every message carries a schema version, a monotonically increasing sequence
number (so consumers can detect gaps), and a UTC ISO-8601 timestamp. Routing-
relevant fields are duplicated into application properties so IoT Hub routing
queries can filter on them without parsing the body.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from azure.iot.device import Message

SCHEMA_VERSION = "1.0"
CONTENT_TYPE = "application/json"
CONTENT_ENCODING = "utf-8"


def build_payload(
    device_id: str,
    sequence: int,
    reading: dict[str, float],
    health: str,
    now: datetime | None = None,
) -> dict:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "deviceId": device_id,
        "sequence": sequence,
        "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "health": health,
        **reading,
    }


def to_message(payload: dict) -> Message:
    message = Message(json.dumps(payload, separators=(",", ":")))
    message.message_id = str(uuid.uuid4())
    message.content_type = CONTENT_TYPE
    message.content_encoding = CONTENT_ENCODING
    message.custom_properties["schemaVersion"] = payload["schemaVersion"]
    message.custom_properties["health"] = payload["health"]
    return message
