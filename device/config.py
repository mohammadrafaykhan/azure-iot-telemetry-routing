"""Device configuration from the environment (or a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CERT_SUFFIX = ".cert.pem"
KEY_SUFFIX = ".key.pem"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class DeviceConfig:
    hub_hostname: str
    certs_dir: Path

    def cert_file(self, device_id: str) -> Path:
        return self.certs_dir / f"{device_id}{CERT_SUFFIX}"

    def key_file(self, device_id: str) -> Path:
        return self.certs_dir / f"{device_id}{KEY_SUFFIX}"

    def discover_devices(self) -> list[str]:
        if not self.certs_dir.is_dir():
            return []
        return sorted(
            path.name.removesuffix(CERT_SUFFIX)
            for path in self.certs_dir.glob(f"*{CERT_SUFFIX}")
            if (self.certs_dir / path.name.replace(CERT_SUFFIX, KEY_SUFFIX)).exists()
        )


def load_config() -> DeviceConfig:
    from dotenv import load_dotenv

    load_dotenv()
    hostname = os.getenv("IOTHUB_HOSTNAME", "").strip().removeprefix("https://").rstrip("/")
    if not hostname:
        raise ConfigError("IOTHUB_HOSTNAME is not set (run infra/deploy.ps1 or see .env.example)")
    return DeviceConfig(hub_hostname=hostname, certs_dir=Path(os.getenv("CERTS_DIR", "certs")))
