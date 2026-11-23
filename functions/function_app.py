"""Azure Functions entry points (Python v2 model). All logic lives in ``pipeline``.

* ``stream``        Event Hubs trigger: routed telemetry and twin changes → archive, rules, presence.
* ``connectivity``  Event Grid trigger: IoT Hub DeviceConnected / DeviceDisconnected.
* ``presence``      Timer: raise offline and silent alerts that are due.

Clients are created on first use, not at import, so a configuration problem shows up
as a failed invocation in the logs instead of the host failing to index the functions.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from functools import cache
from typing import Any, List  # noqa: UP035 (List: see stream)

import azure.functions as func
from azure.identity import DefaultAzureCredential

from pipeline.events import InvalidEvent, RoutedEvent, parse
from pipeline.processor import Processor, check_presence, handle_connection
from pipeline.settings import Settings, load_settings
from pipeline.store import AzureStore

app = func.FunctionApp()
logger = logging.getLogger("telemetry.functions")
# The Azure SDKs log every HTTP request and response at INFO, which would flood Application
# Insights with several traces per Storage call.
logging.getLogger("azure").setLevel(logging.WARNING)


@cache
def _settings() -> Settings:
    return load_settings()


@cache
def _store() -> AzureStore:
    return AzureStore(_settings().storage_account, DefaultAzureCredential())


def _application_properties(events: list[func.EventHubEvent]) -> list[dict[str, Any]]:
    """Per-event application properties (enrichments), from the batch's trigger metadata."""
    metadata = (events[0].metadata or {}) if events else {}
    raw = metadata.get("PropertiesArray")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list) and len(raw) == len(events):
        return [item if isinstance(item, dict) else {} for item in raw]
    return [{} for _ in events]


def to_routed(events: list[func.EventHubEvent]) -> list[RoutedEvent]:
    routed: list[RoutedEvent] = []
    for event, properties in zip(events, _application_properties(events), strict=True):
        try:
            routed.append(
                parse(
                    event.get_body(),
                    event.iothub_metadata or {},
                    properties,
                    enqueued_at=event.enqueued_time or datetime.now(UTC),
                    sequence_number=event.sequence_number or 0,
                    offset=event.offset or "",
                )
            )
        except InvalidEvent as exc:
            logger.warning("skipping event %s: %s", event.sequence_number, exc)
    return routed


@app.function_name("stream")
@app.retry(
    strategy="exponential_backoff",
    max_retry_count="5",
    minimum_interval="00:00:05",
    maximum_interval="00:02:00",
)
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="%EVENTHUB_NAME%",
    connection="EventHubConnection",
    cardinality=func.Cardinality.MANY,
)
def stream(events: List[func.EventHubEvent]) -> None:  # noqa: UP006 (worker rejects list[...])
    routed = to_routed(events)
    alerts = Processor(_store(), _settings().rules).process(routed)
    logger.info("processed %d of %d events, %d alert transitions", len(routed), len(events), len(alerts))


@app.function_name("connectivity")
@app.event_grid_trigger(arg_name="event")
def connectivity(event: func.EventGridEvent) -> None:
    try:
        handle_connection(_store(), event.event_type, event.get_json(), event.event_time or datetime.now(UTC))
    except InvalidEvent as exc:
        logger.warning("skipping Event Grid event %s: %s", event.id, exc)


@app.function_name("presence")
@app.timer_trigger(arg_name="timer", schedule="0 */1 * * * *", run_on_startup=False, use_monitor=False)
def presence(timer: func.TimerRequest) -> None:
    check_presence(_store(), datetime.now(UTC), _settings().presence)
