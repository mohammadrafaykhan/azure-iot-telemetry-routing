from datetime import UTC, datetime, timedelta

from pipeline.presence import (
    Presence,
    PresencePolicy,
    apply_connection,
    check,
    is_newer,
    note_interval,
    note_telemetry,
)

T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
POLICY = PresencePolicy(offline_after=timedelta(minutes=2), silent_floor=timedelta(minutes=2))
SEQ1 = "000000000000000001D4132452F67CE200000002000000000000000000000001"
SEQ2 = "000000000000000001D4132452F67CE200000002000000000000000000000002"
SEQ10 = "000000000000000001D4132452F67CE20000000200000000000000000000000A"


def test_sequence_numbers_compare_as_hex():
    assert is_newer(SEQ2, SEQ1) and is_newer(SEQ10, SEQ2)
    assert not is_newer(SEQ1, SEQ2) and not is_newer(SEQ2, SEQ2)
    assert is_newer(SEQ1, "") and not is_newer("not-hex", SEQ1)


def test_out_of_order_connection_events_are_ignored():
    presence, _ = apply_connection(Presence("press-01"), False, SEQ2, T0)
    stale, alerts = apply_connection(presence, True, SEQ1, T0 + timedelta(seconds=5))
    assert stale == presence and alerts == []
    assert presence.connected is False and presence.changed_at == T0


def test_offline_alert_after_the_grace_period_then_resolved_on_reconnect():
    presence, _ = apply_connection(Presence("press-01"), False, SEQ1, T0)
    early, alerts = check(presence, T0 + timedelta(seconds=90), POLICY)
    assert alerts == [] and early == presence

    offline, alerts = check(presence, T0 + timedelta(minutes=3), POLICY)
    assert [(a.kind, a.state, a.severity) for a in alerts] == [("offline", "raised", "critical")]
    assert "3.0 min" in alerts[0].message and offline.offline_alerted

    again, alerts = check(offline, T0 + timedelta(minutes=10), POLICY)
    assert alerts == [] and again == offline

    back, alerts = apply_connection(offline, True, SEQ2, T0 + timedelta(minutes=11))
    assert [(a.kind, a.state) for a in alerts] == [("offline", "resolved")]
    assert back.connected and not back.offline_alerted


def test_reconnect_before_the_grace_period_raises_nothing():
    down, _ = apply_connection(Presence("press-01"), False, SEQ1, T0)
    up, alerts = apply_connection(down, True, SEQ2, T0 + timedelta(seconds=40))
    assert alerts == []
    assert check(up, T0 + timedelta(minutes=1), POLICY)[1] == []


def test_silent_device_uses_three_reported_intervals():
    presence, _ = apply_connection(Presence("press-01"), True, SEQ1, T0)
    presence = note_interval(presence, 60_000)
    presence, _ = note_telemetry(presence, T0 + timedelta(minutes=1))
    assert check(presence, T0 + timedelta(minutes=3, seconds=59), POLICY)[1] == []

    silent, alerts = check(presence, T0 + timedelta(minutes=4), POLICY)
    assert [(a.kind, a.state, a.severity) for a in alerts] == [("silent", "raised", "warning")]
    assert silent.silent_alerted

    resumed, alerts = note_telemetry(silent, T0 + timedelta(minutes=5))
    assert [(a.kind, a.state) for a in alerts] == [("silent", "resolved")]
    assert not resumed.silent_alerted and resumed.last_seen == T0 + timedelta(minutes=5)


def test_fast_devices_still_get_the_silent_floor():
    presence, _ = apply_connection(Presence("press-01"), True, SEQ1, T0)
    presence = note_interval(presence, 5_000)
    presence, _ = note_telemetry(presence, T0)
    assert check(presence, T0 + timedelta(seconds=119), POLICY)[1] == []
    assert check(presence, T0 + timedelta(seconds=120), POLICY)[1][0].kind == "silent"


def test_connected_but_never_reporting_is_silent():
    presence, _ = apply_connection(Presence("press-01"), True, SEQ1, T0)
    _, alerts = check(presence, T0 + timedelta(minutes=3), POLICY)
    assert alerts[0].message == "connected but no telemetry"


def test_unknown_or_disconnected_devices_are_not_silent():
    assert check(Presence("press-01", last_seen=T0), T0 + timedelta(hours=1), POLICY)[1] == []


def test_older_telemetry_does_not_move_last_seen_back():
    presence, _ = note_telemetry(Presence("press-01"), T0)
    same, alerts = note_telemetry(presence, T0 - timedelta(minutes=1))
    assert same == presence and alerts == []
