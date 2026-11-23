"""Routed IoT Hub messages as they arrive from Event Hubs.

IoT Hub stamps each routed message with system properties (``iothub-*``); the
Functions host exposes them without the prefix. Two routes feed the event hub:
device telemetry (source ``Telemetry``) and twin change notifications (source
``twinChangeEvents``). Message enrichments arrive as application properties.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pipeline.model import utc

TELEMETRY = "Telemetry"
TWIN_CHANGE = "twinChangeEvents"


class InvalidEvent(ValueError):
    """A routed message that cannot be processed; it is logged and skipped."""


@dataclass(frozen=True)
class RoutedEvent:
    source: str
    device_id: str
    enqueued_at: datetime
    sequence_number: int
    offset: str
    body: dict[str, Any]
    properties: dict[str, str] = field(default_factory=dict)

    def reported_interval_ms(self) -> int | None:
        """telemetryIntervalMs from a twin change, whether a plain value or a Plug and Play ack."""
        reported = (self.body.get("properties") or {}).get("reported") or {}
        value = reported.get("telemetryIntervalMs")
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    def number(self, name: str) -> float | None:
        value = self.body.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse(
    body: bytes,
    system: Mapping[str, Any],
    properties: Mapping[str, Any] | None,
    *,
    enqueued_at: datetime,
    sequence_number: int,
    offset: Any,
) -> RoutedEvent:
    device_id = system.get("connection-device-id")
    source = system.get("message-source")
    if not device_id or not source:
        raise InvalidEvent("missing iothub-connection-device-id or iothub-message-source")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidEvent(f"body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidEvent("body is not a JSON object")
    return RoutedEvent(
        source=_text(source),
        device_id=_text(device_id),
        enqueued_at=utc(enqueued_at),
        sequence_number=int(sequence_number),
        offset=_text(offset),
        body=payload,
        properties={str(k): _text(v) for k, v in (properties or {}).items()},
    )
