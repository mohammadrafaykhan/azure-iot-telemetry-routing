"""Threshold alert rules with debounce and hysteresis.

A rule watches one telemetry metric and has ascending severity levels. Each level
raises at ``raise_at`` and is held until the value falls below ``clear_below``, so a
reading hovering around a threshold does not flap. Any change of severity must be
seen in ``samples_to_raise`` consecutive readings before it takes effect, which
absorbs single noisy samples. Pure functions: no I/O.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime

from pipeline.model import SEVERITIES, Alert, rank


@dataclass(frozen=True)
class Level:
    severity: str
    raise_at: float
    clear_below: float


@dataclass(frozen=True)
class Rule:
    metric: str
    unit: str
    levels: tuple[Level, ...]
    samples_to_raise: int = 2

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError(f"{self.metric}: at least one level is required")
        if self.samples_to_raise < 1:
            raise ValueError(f"{self.metric}: samples_to_raise must be at least 1")
        previous = 0
        for level in self.levels:
            if level.severity not in SEVERITIES[1:]:
                raise ValueError(f"{self.metric}: unknown severity {level.severity!r}")
            if rank(level.severity) <= previous:
                raise ValueError(f"{self.metric}: levels must be in ascending severity")
            if not level.clear_below < level.raise_at:
                raise ValueError(f"{self.metric}: clear_below must be below raise_at")
            previous = rank(level.severity)
        raises = [level.raise_at for level in self.levels]
        if raises != sorted(raises):
            raise ValueError(f"{self.metric}: raise_at must increase with severity")

    def level(self, severity: str) -> Level | None:
        return next((level for level in self.levels if level.severity == severity), None)


@dataclass(frozen=True)
class RuleState:
    severity: str = "ok"
    pending: str = "ok"
    pending_count: int = 0
    last_at: datetime | None = None
    since: datetime | None = None


# Vibration zones follow ISO 10816-3 boundaries for medium machines (4.5 and 7.1 mm/s RMS).
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        "vibration_mm_s",
        "mm/s",
        (Level("warning", 4.5, 4.0), Level("critical", 7.1, 6.5)),
    ),
    Rule(
        "temperature_c",
        "°C",
        (Level("warning", 80.0, 78.0), Level("critical", 88.0, 85.0)),
    ),
)


def target_severity(rule: Rule, current: str, value: float) -> str:
    """Severity the value calls for, given the severity currently held."""
    escalate = max((rank(lv.severity) for lv in rule.levels if value >= lv.raise_at), default=0)
    if escalate > rank(current):
        return SEVERITIES[escalate]
    hold = max(
        (
            rank(lv.severity)
            for lv in rule.levels
            if rank(lv.severity) <= rank(current) and value >= lv.clear_below
        ),
        default=0,
    )
    return SEVERITIES[hold]


def _describe(rule: Rule, previous: str, current: str, value: float) -> tuple[str, float, str]:
    if rank(current) > rank(previous):
        state = "raised" if previous == "ok" else "escalated"
        threshold = rule.level(current).raise_at  # type: ignore[union-attr]
        relation = "≥"
    else:
        state = "cleared" if current == "ok" else "eased"
        threshold = rule.level(previous).clear_below  # type: ignore[union-attr]
        relation = "<"
    message = f"{rule.metric} {value:g} {rule.unit} {relation} {threshold:g} ({previous} → {current})"
    return state, threshold, message


def evaluate(
    rule: Rule, state: RuleState, device_id: str, value: float, at: datetime
) -> tuple[RuleState, Alert | None]:
    """Apply one reading. Readings at or before the last one seen are replays and are ignored."""
    if not math.isfinite(value) or (state.last_at is not None and at <= state.last_at):
        return state, None
    target = target_severity(rule, state.severity, value)
    if target == state.severity:
        return replace(state, pending=target, pending_count=0, last_at=at), None
    count = state.pending_count + 1 if state.pending == target else 1
    if count < rule.samples_to_raise:
        return replace(state, pending=target, pending_count=count, last_at=at), None
    alert_state, threshold, message = _describe(rule, state.severity, target, value)
    alert = Alert(
        device_id=device_id,
        kind="threshold",
        subject=rule.metric,
        state=alert_state,
        severity=target if target != "ok" else state.severity,
        message=message,
        at=at,
        value=value,
        threshold=threshold,
    )
    return RuleState(severity=target, pending=target, pending_count=0, last_at=at, since=at), alert


def load_rules(text: str | None) -> tuple[Rule, ...]:
    """Rules from JSON (the ALERT_RULES app setting), or the defaults when unset.

    [{"metric": "vibration_mm_s", "unit": "mm/s", "samplesToRaise": 2,
      "levels": [{"severity": "warning", "raiseAt": 4.5, "clearBelow": 4.0}]}]
    """
    if not text or not text.strip():
        return DEFAULT_RULES
    try:
        raw = json.loads(text)
        rules = tuple(
            Rule(
                metric=str(item["metric"]),
                unit=str(item.get("unit", "")),
                samples_to_raise=int(item.get("samplesToRaise", 2)),
                levels=tuple(
                    Level(str(lv["severity"]), float(lv["raiseAt"]), float(lv["clearBelow"]))
                    for lv in item["levels"]
                ),
            )
            for item in raw
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid ALERT_RULES: {exc}") from exc
    metrics = [rule.metric for rule in rules]
    if len(set(metrics)) != len(metrics):
        raise ValueError("invalid ALERT_RULES: duplicate metric")
    return rules
