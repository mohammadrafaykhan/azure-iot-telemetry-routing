"""Device presence from two signals, as Microsoft recommends for reliable presence.

* Connection state from IoT Hub's DeviceConnected/DeviceDisconnected events (Event
  Grid). Event Grid does not guarantee order, so each event's hexadecimal
  ``sequenceNumber`` decides whether it is newer than the state already held.
* Last-seen time from routed telemetry, with the interval the device reports in
  its twin, to catch a device that is connected but has stopped sending.

A device is **offline** once it has been disconnected for ``offline_after``, and
**silent** once it is connected but nothing has arrived for three intervals (at
least ``silent_floor``). Pure functions: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from pipeline.model import Alert

DEFAULT_INTERVAL_MS = 60_000
SILENT_INTERVALS = 3


@dataclass(frozen=True)
class Presence:
    device_id: str
    connected: bool | None = None
    sequence: str = ""
    changed_at: datetime | None = None
    last_seen: datetime | None = None
    interval_ms: int | None = None
    offline_alerted: bool = False
    silent_alerted: bool = False


@dataclass(frozen=True)
class PresencePolicy:
    offline_after: timedelta = timedelta(minutes=2)
    silent_floor: timedelta = timedelta(minutes=2)

    def silent_after(self, interval_ms: int | None) -> timedelta:
        interval = timedelta(milliseconds=interval_ms or DEFAULT_INTERVAL_MS)
        return max(interval * SILENT_INTERVALS, self.silent_floor)


def is_newer(candidate: str, current: str) -> bool:
    """Compare IoT Hub connection-state sequence numbers (hexadecimal strings)."""
    if not current:
        return True
    try:
        return int(candidate, 16) > int(current, 16)
    except ValueError:
        return False


def _alert(device_id: str, kind: str, state: str, message: str, at: datetime) -> Alert:
    subject = "connection" if kind == "offline" else "telemetry"
    severity = "critical" if kind == "offline" else "warning"
    return Alert(device_id, kind, subject, state, severity, message, at)


def apply_connection(
    presence: Presence, connected: bool, sequence: str, at: datetime
) -> tuple[Presence, list[Alert]]:
    if not is_newer(sequence, presence.sequence):
        return presence, []
    updated = replace(presence, connected=connected, sequence=sequence, changed_at=at)
    alerts: list[Alert] = []
    if connected and presence.offline_alerted:
        updated = replace(updated, offline_alerted=False)
        alerts.append(_alert(presence.device_id, "offline", "resolved", "device reconnected", at))
    return updated, alerts


def note_telemetry(presence: Presence, at: datetime) -> tuple[Presence, list[Alert]]:
    if presence.last_seen is not None and at <= presence.last_seen:
        return presence, []
    updated = replace(presence, last_seen=at)
    alerts: list[Alert] = []
    if presence.silent_alerted:
        updated = replace(updated, silent_alerted=False)
        alerts.append(_alert(presence.device_id, "silent", "resolved", "telemetry resumed", at))
    return updated, alerts


def note_interval(presence: Presence, interval_ms: int) -> Presence:
    return replace(presence, interval_ms=interval_ms)


def check(presence: Presence, now: datetime, policy: PresencePolicy) -> tuple[Presence, list[Alert]]:
    """Raise offline or silent alerts that are due."""
    if presence.connected is False and not presence.offline_alerted and presence.changed_at:
        down_for = now - presence.changed_at
        if down_for >= policy.offline_after:
            minutes = down_for.total_seconds() / 60
            message = f"disconnected for {minutes:.1f} min"
            alert = _alert(presence.device_id, "offline", "raised", message, now)
            return replace(presence, offline_alerted=True), [alert]
    if presence.connected and not presence.silent_alerted:
        reference = presence.last_seen or presence.changed_at
        limit = policy.silent_after(presence.interval_ms)
        if reference is not None and now - reference >= limit:
            if presence.last_seen:
                message = f"no telemetry for {(now - reference).total_seconds() / 60:.1f} min"
            else:
                message = "connected but no telemetry"
            alert = _alert(presence.device_id, "silent", "raised", message, now)
            return replace(presence, silent_alerted=True), [alert]
    return presence, []
