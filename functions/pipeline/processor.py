"""Orchestration for the three functions: routed batches, connection events, presence checks.

Event Hubs delivers at least once, so every step is idempotent: the archive blob
name and alert row keys are derived from the events themselves, and rule state
ignores readings it has already seen. Alerts are written before the state that
produced them, so a failure in between can only repeat an alert, never lose one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from itertools import groupby
from typing import Any

from pipeline.archive import blob_name, to_jsonl
from pipeline.events import TELEMETRY, TWIN_CHANGE, InvalidEvent, RoutedEvent
from pipeline.model import Alert, utc
from pipeline.presence import (
    Presence,
    PresencePolicy,
    apply_connection,
    check,
    note_interval,
    note_telemetry,
)
from pipeline.rules import DEFAULT_RULES, Rule, RuleState, evaluate
from pipeline.store import Conflict, Store

logger = logging.getLogger("telemetry.pipeline")

PRESENCE_ATTEMPTS = 4
CONNECTED = "Microsoft.Devices.DeviceConnected"
DISCONNECTED = "Microsoft.Devices.DeviceDisconnected"

PresenceChange = Callable[[Presence], tuple[Presence, list[Alert]]]


def log_alerts(alerts: Iterable[Alert]) -> None:
    """One trace per alert; the Azure Monitor alert rule matches 'ALERT' traces at warning level."""
    for alert in alerts:
        logger.log(
            logging.WARNING if alert.notifies else logging.INFO,
            "ALERT %s %s %s/%s %s: %s",
            alert.severity.upper(),
            alert.device_id,
            alert.kind,
            alert.subject,
            alert.state,
            alert.message,
        )


def update_presence(store: Store, device_id: str, change: PresenceChange) -> list[Alert]:
    """Read-modify-write a presence row with optimistic concurrency."""
    for _ in range(PRESENCE_ATTEMPTS):
        current, etag = store.presence(device_id)
        updated, alerts = change(current)
        if updated == current:
            return []
        store.add_alerts(alerts)
        try:
            store.save_presence(updated, etag)
        except Conflict:
            continue
        return alerts
    raise Conflict(f"{device_id}: presence kept changing during update")


class Processor:
    def __init__(self, store: Store, rules: Sequence[Rule] = DEFAULT_RULES) -> None:
        self._store = store
        self._rules = tuple(rules)

    def process(self, events: Sequence[RoutedEvent]) -> list[Alert]:
        if not events:
            return []
        self._store.archive(blob_name(events), to_jsonl(events))
        ordered = sorted(events, key=lambda e: (e.device_id, e.enqueued_at, e.sequence_number))
        alerts: list[Alert] = []
        for device_id, group in groupby(ordered, key=lambda e: e.device_id):
            batch = list(group)
            alerts += self._apply_rules(device_id, [e for e in batch if e.source == TELEMETRY])
            alerts += self._update_presence(device_id, batch)
        log_alerts(alerts)
        return alerts

    def _apply_rules(self, device_id: str, telemetry: list[RoutedEvent]) -> list[Alert]:
        if not telemetry:
            return []
        states = self._store.rule_states(device_id)
        before = dict(states)
        alerts: list[Alert] = []
        for event in telemetry:
            for rule in self._rules:
                value = event.number(rule.metric)
                if value is None:
                    continue
                state = states.get(rule.metric, RuleState())
                states[rule.metric], alert = evaluate(rule, state, device_id, value, event.enqueued_at)
                if alert is not None:
                    alerts.append(alert)
        self._store.add_alerts(alerts)
        self._store.save_rule_states(
            device_id, {metric: s for metric, s in states.items() if before.get(metric) != s}
        )
        return alerts

    def _update_presence(self, device_id: str, batch: list[RoutedEvent]) -> list[Alert]:
        last_seen = max((e.enqueued_at for e in batch if e.source == TELEMETRY), default=None)
        intervals = [e.reported_interval_ms() for e in batch if e.source == TWIN_CHANGE]
        interval = next((value for value in reversed(intervals) if value is not None), None)
        if last_seen is None and interval is None:
            return []

        def change(presence: Presence) -> tuple[Presence, list[Alert]]:
            if interval is not None:
                presence = note_interval(presence, interval)
            if last_seen is not None:
                return note_telemetry(presence, last_seen)
            return presence, []

        return update_presence(self._store, device_id, change)


def parse_connection_event(
    event_type: str, data: Mapping[str, Any], event_time: datetime
) -> tuple[str, bool, str, datetime]:
    """(device id, connected, sequence number, time) from an IoT Hub connection-state event."""
    if event_type not in (CONNECTED, DISCONNECTED):
        raise InvalidEvent(f"unexpected event type {event_type!r}")
    device_id = data.get("deviceId")
    info = data.get("deviceConnectionStateEventInfo") or {}
    sequence = info.get("sequenceNumber")
    if not device_id or not sequence:
        raise InvalidEvent("connection event without deviceId or sequenceNumber")
    return str(device_id), event_type == CONNECTED, str(sequence), utc(event_time)


def handle_connection(
    store: Store, event_type: str, data: Mapping[str, Any], event_time: datetime
) -> list[Alert]:
    if data.get("moduleId"):
        return []
    device_id, connected, sequence, at = parse_connection_event(event_type, data, event_time)
    alerts = update_presence(store, device_id, lambda p: apply_connection(p, connected, sequence, at))
    log_alerts(alerts)
    return alerts


def check_presence(store: Store, now: datetime, policy: PresencePolicy) -> list[Alert]:
    alerts: list[Alert] = []
    for presence, _ in store.all_presence():
        try:
            alerts += update_presence(store, presence.device_id, lambda p: check(p, now, policy))
        except Conflict:
            logger.info("presence of %s changed during the check; next run re-evaluates", presence.device_id)
    log_alerts(alerts)
    return alerts
