import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fakes import T0, telemetry

from pipeline.archive import blob_name, to_jsonl
from pipeline.events import TELEMETRY, TWIN_CHANGE, InvalidEvent, RoutedEvent, parse
from pipeline.model import Alert
from pipeline.presence import Presence
from pipeline.rules import RuleState
from pipeline.settings import SettingsError, load_settings
from pipeline.store import (
    alert_entity,
    presence_entity,
    presence_from,
    rule_state_entity,
    rule_state_from,
    table_key,
)

SYSTEM = {"connection-device-id": "press-01", "message-source": TELEMETRY}


def test_parse_routed_telemetry():
    event = parse(
        b'{"schemaVersion":"1.0","vibration_mm_s":2.5,"running":true}',
        SYSTEM,
        {"site": "karachi", "line": b"1"},
        enqueued_at=datetime(2026, 11, 23, 12, 0),
        sequence_number=7,
        offset=1234,
    )
    assert event.device_id == "press-01" and event.source == TELEMETRY
    assert event.enqueued_at.tzinfo is UTC and event.offset == "1234"
    assert event.properties == {"site": "karachi", "line": "1"}
    assert event.number("vibration_mm_s") == 2.5
    assert event.number("running") is None and event.number("missing") is None


@pytest.mark.parametrize(
    ("body", "system"),
    [
        (b"not json", SYSTEM),
        (b"[1, 2]", SYSTEM),
        (b"{}", {"message-source": TELEMETRY}),
        (b"{}", {"connection-device-id": "press-01"}),
    ],
)
def test_parse_rejects_unusable_events(body, system):
    with pytest.raises(InvalidEvent):
        parse(body, system, None, enqueued_at=T0, sequence_number=1, offset=1)


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ({"telemetryIntervalMs": {"value": 10000, "ac": 200}}, 10000),
        ({"telemetryIntervalMs": 30000}, 30000),
        ({"telemetryIntervalMs": {"value": True}}, None),
        ({"telemetryIntervalMs": -5}, None),
        ({"firmwareVersion": "1.0.0"}, None),
    ],
)
def test_reported_interval_from_twin_changes(reported, expected):
    event = RoutedEvent(TWIN_CHANGE, "press-01", T0, 1, "1", {"properties": {"reported": reported}})
    assert event.reported_interval_ms() == expected


def test_archive_name_is_deterministic_and_position_based():
    a, b = telemetry(at=T0), telemetry(at=T0 + timedelta(seconds=30))
    assert blob_name([a, b]) == blob_name([a, b])
    assert blob_name([a, b]) != blob_name([a])
    assert blob_name([b, a]).startswith("raw/2026/11/23/12/20261123T120000Z-")
    with pytest.raises(ValueError):
        blob_name([])


def test_archive_lines_are_json_with_unicode_kept():
    event = RoutedEvent(TELEMETRY, "presse-ü", T0, 1, "1", {"note": "°C"})
    (line,) = to_jsonl([event]).decode("utf-8").splitlines()
    assert json.loads(line)["deviceId"] == "presse-ü" and "°C" in line


def test_table_keys_escape_forbidden_characters():
    assert table_key("press-01") == "press-01"
    assert table_key("a/b\\c#d?e%f") == "a%2Fb%5Cc%23d%3Fe%25f"


def test_rule_state_round_trip():
    state = RuleState("warning", "critical", 1, T0, T0 - timedelta(minutes=5))
    entity = rule_state_entity("press?01", "vibration_mm_s", state)
    assert entity["PartitionKey"] == "press%3F01" and entity["deviceId"] == "press?01"
    assert rule_state_from(entity) == state
    assert "since" not in rule_state_entity("p", "m", RuleState())


def test_presence_round_trip_and_unknown_connection():
    presence = Presence("press-01", True, "0A", T0, T0, 60000, False, True)
    assert presence_from(presence_entity(presence)) == presence
    unknown = presence_entity(Presence("press-02"))
    assert "connected" not in unknown and "sequence" not in unknown
    assert presence_from(unknown) == Presence("press-02")


def test_presence_times_are_normalised_to_utc():
    entity = presence_entity(Presence("p", last_seen=T0))
    entity["lastSeen"] = T0.astimezone(timezone(timedelta(hours=5)))
    assert presence_from(entity).last_seen == T0


def test_alert_rows_sort_newest_first_within_a_day():
    early = alert_entity(
        Alert("press-01", "threshold", "vibration_mm_s", "raised", "warning", "m", T0, 4.6, 4.5)
    )
    later = alert_entity(
        Alert("press-01", "offline", "connection", "raised", "critical", "m", T0 + timedelta(minutes=1))
    )
    assert early["PartitionKey"] == later["PartitionKey"] == "20261123"
    assert later["RowKey"] < early["RowKey"]
    assert "value" not in later and early["threshold"] == 4.5


def test_settings_from_environment():
    settings = load_settings({"STORAGE_ACCOUNT_NAME": "st1", "OFFLINE_AFTER_SECONDS": "300"})
    assert settings.storage_account == "st1"
    assert settings.presence.offline_after == timedelta(minutes=5)
    assert settings.presence.silent_floor == timedelta(minutes=2)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"STORAGE_ACCOUNT_NAME": "st1", "OFFLINE_AFTER_SECONDS": "ten"},
        {"STORAGE_ACCOUNT_NAME": "st1", "SILENT_FLOOR_SECONDS": "5"},
    ],
)
def test_invalid_settings_are_rejected(env):
    with pytest.raises(SettingsError):
        load_settings(env)
