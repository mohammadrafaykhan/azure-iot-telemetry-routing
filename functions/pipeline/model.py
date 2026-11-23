"""Alert records shared by the threshold rules and the presence checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

SEVERITIES: tuple[str, ...] = ("ok", "warning", "critical")


def rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def utc(value: datetime) -> datetime:
    """Normalise to an aware UTC datetime (naive values are taken as UTC)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value: datetime) -> str:
    return utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Alert:
    """One alert transition.

    ``kind`` is ``threshold``, ``offline`` or ``silent``; ``state`` is ``raised``,
    ``escalated``, ``eased``, ``cleared`` (threshold) or ``raised``/``resolved`` (presence).
    """

    device_id: str
    kind: str
    subject: str
    state: str
    severity: str
    message: str
    at: datetime
    value: float | None = None
    threshold: float | None = None

    @property
    def notifies(self) -> bool:
        """Transitions that make things worse are the ones an operator is told about."""
        return self.state in ("raised", "escalated")
