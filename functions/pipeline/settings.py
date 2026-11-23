"""Function app settings, read from the environment (app settings in Azure)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from pipeline.presence import PresencePolicy
from pipeline.rules import Rule, load_rules


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    storage_account: str
    rules: tuple[Rule, ...]
    presence: PresencePolicy


def _seconds(env: Mapping[str, str], name: str, default: int) -> timedelta:
    raw = env.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise SettingsError(f"{name} must be a whole number of seconds") from exc
    if not 30 <= value <= 86_400:
        raise SettingsError(f"{name} must be between 30 and 86400")
    return timedelta(seconds=value)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    account = env.get("STORAGE_ACCOUNT_NAME", "").strip()
    if not account:
        raise SettingsError("STORAGE_ACCOUNT_NAME is not set")
    return Settings(
        storage_account=account,
        rules=load_rules(env.get("ALERT_RULES")),
        presence=PresencePolicy(
            offline_after=_seconds(env, "OFFLINE_AFTER_SECONDS", 120),
            silent_floor=_seconds(env, "SILENT_FLOOR_SECONDS", 120),
        ),
    )
