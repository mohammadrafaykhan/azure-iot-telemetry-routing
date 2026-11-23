"""Raw archive: every routed batch as one JSON Lines blob, partitioned by hour.

The blob name is derived from the batch's Event Hubs positions, so a batch that
is delivered again after a failure overwrites the same blob instead of
duplicating it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from pipeline.events import RoutedEvent
from pipeline.model import iso

PREFIX = "raw"


def blob_name(events: Sequence[RoutedEvent]) -> str:
    if not events:
        raise ValueError("cannot archive an empty batch")
    first = min(event.enqueued_at for event in events)
    positions = "|".join(f"{e.sequence_number}:{e.offset}" for e in events)
    digest = hashlib.sha256(positions.encode("ascii")).hexdigest()[:16]
    return f"{PREFIX}/{first:%Y/%m/%d/%H}/{first:%Y%m%dT%H%M%S}Z-{digest}.jsonl"


def to_jsonl(events: Sequence[RoutedEvent]) -> bytes:
    lines = (
        json.dumps(
            {
                "enqueuedAt": iso(event.enqueued_at),
                "source": event.source,
                "deviceId": event.device_id,
                "sequenceNumber": event.sequence_number,
                "properties": event.properties,
                "body": event.body,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for event in events
    )
    return ("\n".join(lines) + "\n").encode("utf-8")
