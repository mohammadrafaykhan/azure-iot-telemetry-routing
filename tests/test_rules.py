from datetime import UTC, datetime, timedelta

import pytest

from pipeline.rules import DEFAULT_RULES, Level, Rule, RuleState, evaluate, load_rules, target_severity

T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
VIBRATION = DEFAULT_RULES[0]


def run(rule, values, state=None, start=T0):
    """Feed readings one minute apart; return final state and the alerts raised."""
    state = state or RuleState()
    alerts = []
    for i, value in enumerate(values):
        state, alert = evaluate(rule, state, "press-01", value, start + timedelta(minutes=i))
        if alert:
            alerts.append(alert)
    return state, alerts


def test_normal_readings_raise_nothing():
    state, alerts = run(VIBRATION, [2.5, 2.7, 3.1, 4.4])
    assert alerts == [] and state.severity == "ok"


def test_a_single_spike_is_absorbed_by_debounce():
    state, alerts = run(VIBRATION, [2.5, 5.0, 2.6, 2.5])
    assert alerts == [] and state.severity == "ok"


def test_two_readings_over_the_threshold_raise_a_warning():
    state, alerts = run(VIBRATION, [2.5, 4.6, 4.8])
    assert [(a.state, a.severity) for a in alerts] == [("raised", "warning")]
    assert alerts[0].threshold == 4.5 and alerts[0].value == 4.8
    assert alerts[0].at == T0 + timedelta(minutes=2)
    assert state.severity == "warning" and state.since == alerts[0].at


def test_escalates_to_critical_and_eases_back_with_hysteresis():
    state, alerts = run(VIBRATION, [4.6, 4.7, 7.2, 7.3, 6.8, 6.7, 6.4, 6.3])
    assert [(a.state, a.severity) for a in alerts] == [
        ("raised", "warning"),
        ("escalated", "critical"),
        ("eased", "warning"),
    ]
    assert alerts[2].threshold == 6.5
    assert state.severity == "warning"


def test_holding_band_prevents_flapping_around_the_threshold():
    _, alerts = run(VIBRATION, [4.6, 4.6, 4.4, 4.6, 4.3, 4.6, 4.2])
    assert [a.state for a in alerts] == ["raised"]


def test_clears_only_below_the_clear_threshold():
    state, alerts = run(VIBRATION, [4.6, 4.7, 4.1, 4.1, 3.9, 3.8])
    assert [a.state for a in alerts] == ["raised", "cleared"]
    assert alerts[1].severity == "warning" and alerts[1].threshold == 4.0
    assert state.severity == "ok"


def test_straight_to_critical_from_ok():
    _, alerts = run(VIBRATION, [7.5, 7.6])
    assert [(a.state, a.severity) for a in alerts] == [("raised", "critical")]


def test_replayed_and_out_of_order_readings_are_ignored():
    state, _ = run(VIBRATION, [4.6, 4.7])
    replay, alert = evaluate(VIBRATION, state, "press-01", 9.9, T0)
    assert alert is None and replay == state


def test_non_finite_values_are_ignored():
    state, alert = evaluate(VIBRATION, RuleState(), "press-01", float("nan"), T0)
    assert alert is None and state == RuleState()


def test_target_severity_holds_and_escalates():
    assert target_severity(VIBRATION, "ok", 4.4) == "ok"
    assert target_severity(VIBRATION, "warning", 4.1) == "warning"
    assert target_severity(VIBRATION, "critical", 6.6) == "critical"
    assert target_severity(VIBRATION, "critical", 6.0) == "warning"
    assert target_severity(VIBRATION, "critical", 3.0) == "ok"
    assert target_severity(VIBRATION, "warning", 7.1) == "critical"


@pytest.mark.parametrize(
    "levels",
    [
        (),
        (Level("warning", 4.0, 4.5),),
        (Level("critical", 7.0, 6.0), Level("warning", 4.5, 4.0)),
        (Level("warning", 8.0, 7.0), Level("critical", 7.5, 7.0)),
        (Level("severe", 8.0, 7.0),),
    ],
)
def test_invalid_rules_are_rejected(levels):
    with pytest.raises(ValueError):
        Rule("x", "", levels)


def test_load_rules_defaults_and_json():
    assert load_rules(None) == DEFAULT_RULES and load_rules("  ") == DEFAULT_RULES
    rules = load_rules(
        '[{"metric": "rpm", "unit": "rpm", "samplesToRaise": 1,'
        ' "levels": [{"severity": "critical", "raiseAt": 1600, "clearBelow": 1550}]}]'
    )
    assert rules == (Rule("rpm", "rpm", (Level("critical", 1600.0, 1550.0),), samples_to_raise=1),)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '[{"metric": "x"}]',
        '[{"metric": "x", "levels": [{"severity": "warning", "raiseAt": 1, "clearBelow": 2}]}]',
        '[{"metric": "x", "levels": [{"severity": "warning", "raiseAt": 2, "clearBelow": 1}]},'
        ' {"metric": "x", "levels": [{"severity": "warning", "raiseAt": 2, "clearBelow": 1}]}]',
    ],
)
def test_load_rules_rejects_bad_configuration(text):
    with pytest.raises(ValueError, match="ALERT_RULES"):
        load_rules(text)
