"""opsctl configuration from the environment (or a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    hub_hostname: str
    storage_account: str
    certs_dir: Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set (run infra/deploy.ps1 or see .env.example)")
    return value


def load_config() -> ServiceConfig:
    from dotenv import load_dotenv

    load_dotenv()
    return ServiceConfig(
        hub_hostname=_required("IOTHUB_HOSTNAME").removeprefix("https://").rstrip("/"),
        storage_account=_required("STORAGE_ACCOUNT_NAME"),
        certs_dir=Path(os.getenv("CERTS_DIR", "certs")),
    )
