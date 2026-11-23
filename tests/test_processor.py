import json
import logging
from datetime import timedelta

import pytest
from fakes import T0, FakeStore, telemetry, twin_change

from pipeline.events import InvalidEvent
from pipeline.presence import Presence, PresencePolicy
from pipeline.processor import (
    CONNECTED,
    DISCONNECTED,
    Processor,
    check_presence,
    handle_connection,
    parse_connection_event,
    update_presence,
)
from pipeline.store import Conflict

SEQ1, SEQ2 = "01", "02"


def minutes(n):
    return T0 + timedelta(minutes=n)


def test_batch_is_archived_once_under_a_deterministic_name():
    store = FakeStore()
    batch = [
        telemetry(at=minutes(0), vibration_mm_s=2.5),
        telemetry("press-02", minutes(0), vibration_mm_s=2.4),
    ]
    Processor(store).process(batch)
    Processor(store).process(batch)
    assert len(store.blobs) == 1
    ((name, data),) = store.blobs.items()
    assert name.startswith("raw/2026/11/23/12/")
    lines = [json.loads(line) for line in data.decode().splitlines()]
    assert [line["deviceId"] for line in lines] == ["press-01", "press-02"]
    assert lines[0]["properties"] == {"site": "karachi"}


def test_threshold_alerts_across_batches_and_devices(caplog):
    store = FakeStore()
    processor = Processor(store)
    processor.process(
        [telemetry(at=minutes(0), vibration_mm_s=4.8), telemetry("press-02", minutes(0), vibration_mm_s=2.5)]
    )
    assert store.alerts == {}
    with caplog.at_level(logging.INFO, logger="telemetry.pipeline"):
        alerts = processor.process([telemetry(at=minutes(1), vibration_mm_s=4.9, temperature_c=70.0)])
    assert [(a.device_id, a.subject, a.state, a.severity) for a in alerts] == [
        ("press-01", "vibration_mm_s", "raised", "warning")
    ]
    assert store.rule_states("press-01")["vibration_mm_s"].severity == "warning"
    assert store.rule_states("press-02")["vibration_mm_s"].severity == "ok"
    record = next(r for r in caplog.records if r.getMessage().startswith("ALERT"))
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "ALERT WARNING press-01 threshold/vibration_mm_s raised: vibration_mm_s 4.9 mm/s ≥ 4.5 (ok → warning)"
    )


def test_redelivered_batch_does_not_duplicate_or_lose_alerts():
    store = FakeStore()
    processor = Processor(store)
    batch = [telemetry(at=minutes(0), vibration_mm_s=7.5), telemetry(at=minutes(1), vibration_mm_s=7.6)]
    first = processor.process(batch)
    again = processor.process(batch)
    assert len(first) == 1 and again == []
    assert len(store.alerts) == 1


def test_alerts_are_written_before_state_so_a_failure_cannot_lose_them():
    store = FakeStore()
    calls = []
    store.add_alerts = lambda alerts, add=store.add_alerts: (calls.append("alerts"), add(alerts))
    store.save_rule_states = lambda d, s, save=store.save_rule_states: (calls.append("state"), save(d, s))
    Processor(store).process(
        [telemetry(at=minutes(0), vibration_mm_s=7.5), telemetry(at=minutes(1), vibration_mm_s=7.6)]
    )
    assert calls.index("alerts") < calls.index("state")


def test_archive_failure_fails_the_batch_for_retry():
    store = FakeStore()
    store.fail_archive = True
    with pytest.raises(OSError):
        Processor(store).process([telemetry(vibration_mm_s=2.5)])


def test_telemetry_updates_last_seen_and_twin_changes_update_interval():
    store = FakeStore()
    Processor(store).process(
        [
            twin_change(at=minutes(0), interval=10_000),
            telemetry(at=minutes(1), vibration_mm_s=2.5),
            telemetry(at=minutes(2), vibration_mm_s=2.5),
        ]
    )
    presence, _ = store.presence("press-01")
    assert presence.last_seen == minutes(2) and presence.interval_ms == 10_000


def test_twin_change_without_interval_leaves_presence_alone():
    store = FakeStore()
    Processor(store).process([twin_change(interval=None)])
    assert store.presences == {}


def test_telemetry_resolves_a_silent_alert():
    store = FakeStore()
    store.save_presence(Presence("press-01", connected=True, silent_alerted=True, last_seen=minutes(0)), None)
    alerts = Processor(store).process([telemetry(at=minutes(9), vibration_mm_s=2.5)])
    assert [(a.kind, a.state) for a in alerts] == [("silent", "resolved")]
    assert not store.presence("press-01")[0].silent_alerted


def test_update_presence_retries_on_conflict_then_gives_up():
    store = FakeStore()
    store.conflicts_left = 2
    update_presence(store, "press-01", lambda p: (Presence(p.device_id, connected=True), []))
    assert store.presence("press-01")[0].connected is True

    store.conflicts_left = 10
    with pytest.raises(Conflict):
        update_presence(store, "press-02", lambda p: (Presence(p.device_id, connected=True), []))


def test_unchanged_presence_is_not_written():
    store = FakeStore()
    assert update_presence(store, "press-01", lambda p: (p, [])) == []
    assert store.saves == 0


def connection(event_type, sequence, device="press-01"):
    return event_type, {
        "deviceId": device,
        "moduleId": "",
        "deviceConnectionStateEventInfo": {"sequenceNumber": sequence},
    }


def test_connection_events_drive_offline_alerts_end_to_end():
    store = FakeStore()
    policy = PresencePolicy(offline_after=timedelta(minutes=2))
    handle_connection(store, *connection(CONNECTED, SEQ1), minutes(0))
    handle_connection(store, *connection(DISCONNECTED, SEQ2), minutes(1))
    assert check_presence(store, minutes(2), policy) == []
    raised = check_presence(store, minutes(4), policy)
    assert [(a.kind, a.state) for a in raised] == [("offline", "raised")]
    assert check_presence(store, minutes(5), policy) == []
    resolved = handle_connection(store, *connection(CONNECTED, "03"), minutes(6))
    assert [(a.kind, a.state) for a in resolved] == [("offline", "resolved")]
    assert len(store.alerts) == 2


def test_late_older_connection_event_does_not_undo_a_newer_one():
    store = FakeStore()
    handle_connection(store, *connection(DISCONNECTED, "05"), minutes(1))
    handle_connection(store, *connection(CONNECTED, "04"), minutes(0))
    assert store.presence("press-01")[0].connected is False


def test_module_connection_events_are_ignored():
    store = FakeStore()
    event_type, data = connection(CONNECTED, SEQ1)
    assert handle_connection(store, event_type, {**data, "moduleId": "edgeAgent"}, minutes(0)) == []
    assert store.presences == {}


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("Microsoft.Devices.DeviceCreated", {"deviceId": "x"}),
        (CONNECTED, {"deviceConnectionStateEventInfo": {"sequenceNumber": "01"}}),
        (CONNECTED, {"deviceId": "x", "deviceConnectionStateEventInfo": {}}),
    ],
)
def test_malformed_connection_events_are_rejected(event_type, data):
    with pytest.raises(InvalidEvent):
        parse_connection_event(event_type, data, T0)
