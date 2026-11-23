"""In-memory stand-ins for the pipeline's storage, with ETag semantics."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from pipeline.events import TELEMETRY, TWIN_CHANGE, RoutedEvent
from pipeline.presence import Presence
from pipeline.store import Conflict

T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
_sequence = itertools.count(1)


class FakeStore:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], object] = {}
        self.presences: dict[str, tuple[Presence, str]] = {}
        self.alerts: dict[tuple, object] = {}
        self.blobs: dict[str, bytes] = {}
        self.conflicts_left = 0
        self.saves = 0
        self.fail_archive = False
        self._etags = itertools.count(1)

    def rule_states(self, device_id):
        return {metric: s for (device, metric), s in self.states.items() if device == device_id}

    def save_rule_states(self, device_id, states):
        for metric, state in states.items():
            self.states[(device_id, metric)] = state

    def presence(self, device_id):
        if device_id not in self.presences:
            return Presence(device_id), None
        presence, etag = self.presences[device_id]
        return presence, etag

    def all_presence(self):
        return list(self.presences.values())

    def save_presence(self, presence, etag):
        current = self.presences.get(presence.device_id)
        if self.conflicts_left or (current and current[1] != etag) or (current is None and etag):
            self.conflicts_left = max(0, self.conflicts_left - 1)
            raise Conflict(presence.device_id)
        self.saves += 1
        self.presences[presence.device_id] = (presence, f"etag-{next(self._etags)}")

    def add_alerts(self, alerts):
        for alert in alerts:
            self.alerts[(alert.device_id, alert.kind, alert.subject, alert.at)] = alert

    def archive(self, name, data):
        if self.fail_archive:
            raise OSError("storage unavailable")
        self.blobs[name] = data

    def alert_list(self):
        return sorted(self.alerts.values(), key=lambda a: a.at)


def telemetry(device_id="press-01", at=T0, **readings) -> RoutedEvent:
    body = {"schemaVersion": "1.0", "deviceId": device_id, **readings}
    seq = next(_sequence)
    return RoutedEvent(TELEMETRY, device_id, at, seq, str(seq * 100), body, {"site": "karachi"})


def twin_change(device_id="press-01", at=T0, interval=None) -> RoutedEvent:
    reported = {"telemetryIntervalMs": {"value": interval, "ac": 200, "av": 2, "ad": "applied"}}
    seq = next(_sequence)
    return RoutedEvent(
        TWIN_CHANGE, device_id, at, seq, str(seq * 100), {"properties": {"reported": reported}}
    )
