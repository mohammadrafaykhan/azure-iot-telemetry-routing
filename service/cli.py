"""opsctl: operate the telemetry pipeline with your Microsoft Entra ID identity.

    opsctl devices add <id>... [--tag k=v ...]   create certificates and register devices
    opsctl devices list                          hub identities, connection state, presence
    opsctl interval (<id>... | --all) <ms>       set the desired telemetry interval
    opsctl maintain <id>                         call CompleteMaintenance (resets wear)
    opsctl alerts [--days N] [--device <id>]     alert history, newest first
    opsctl active                                alerts currently held
    opsctl archive [--hours N] [--show N]        raw archive blobs and the latest records

Results are JSON on stdout; errors go to stderr with exit code 1 (operation failed)
or 2 (invalid input or configuration).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from azure.core.exceptions import AzureError, ClientAuthenticationError

from service.certs import ensure_identity, thumbprint, validate_device_id
from service.config import ConfigError, ServiceConfig, load_config
from service.data import OpsData, Reader
from service.hub import HUB_API_VERSION, HUB_SCOPE, HubRegistry
from service.rest import ApiError, RestClient

logger = logging.getLogger("opsctl")

MAINTENANCE_METHOD = "CompleteMaintenance"
MIN_INTERVAL_MS, MAX_INTERVAL_MS = 1_000, 3_600_000


@dataclass
class Services:
    hub: HubRegistry
    data: Reader


ServicesFactory = Callable[[ServiceConfig], AbstractContextManager[Services]]


def _tag(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key or not value:
        raise argparse.ArgumentTypeError("expected key=value")
    return key, value


def _device_id(text: str) -> str:
    try:
        return validate_device_id(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _interval(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected milliseconds as a whole number") from exc
    if not MIN_INTERVAL_MS <= value <= MAX_INTERVAL_MS:
        raise argparse.ArgumentTypeError(f"must be between {MIN_INTERVAL_MS} and {MAX_INTERVAL_MS}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opsctl", description="Operate the IoT telemetry pipeline.")
    parser.add_argument("-v", "--verbose", action="store_true", help="log HTTP requests")
    commands = parser.add_subparsers(dest="command", required=True)

    devices = commands.add_parser("devices", help="device identities").add_subparsers(
        dest="action", required=True
    )
    add = devices.add_parser("add", help="create certificates and register devices")
    add.add_argument("device_ids", nargs="+", type=_device_id)
    add.add_argument("--tag", type=_tag, action="append", default=[], help="twin tag key=value")
    add.add_argument("--days", type=int, default=365, help="certificate validity for new devices")
    devices.add_parser("list", help="devices with connection state and presence")

    interval = commands.add_parser("interval", help="set the desired telemetry interval")
    interval.add_argument("targets", nargs="*", type=_device_id)
    interval.add_argument("--all", action="store_true", help="every device in the hub")
    interval.add_argument("--ms", type=_interval, required=True)

    maintain = commands.add_parser("maintain", help="record maintenance on a press (resets wear)")
    maintain.add_argument("device_id", type=_device_id)

    alerts = commands.add_parser("alerts", help="alert history, newest first")
    alerts.add_argument("--days", type=int, default=1)
    alerts.add_argument("--device", type=_device_id)

    commands.add_parser("active", help="alerts currently held")

    archive = commands.add_parser("archive", help="raw archive summary and latest records")
    archive.add_argument("--hours", type=int, default=1)
    archive.add_argument("--show", type=int, default=3, help="latest records to print")
    return parser


@contextmanager
def _default_services(config: ServiceConfig) -> Iterator[Services]:
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    try:
        with RestClient(f"https://{config.hub_hostname}", credential, HUB_SCOPE, HUB_API_VERSION) as hub:
            yield Services(hub=HubRegistry(hub), data=OpsData(config.storage_account, credential))
    finally:
        credential.close()


def _execute(args: argparse.Namespace, services: Services, config: ServiceConfig) -> Any:
    now = datetime.now(UTC)
    if args.command == "devices":
        if args.action == "list":
            presence = {row["deviceId"]: row for row in services.data.presence()}
            return [
                {**device, "presence": presence.get(device["deviceId"])}
                for device in services.hub.list_devices()
            ]
        results = []
        for device_id in args.device_ids:
            cert, created = ensure_identity(config.certs_dir, device_id, args.days)
            result = services.hub.register(device_id, thumbprint(cert), dict(args.tag))
            results.append({**result, "certificate": "created" if created else "existing"})
        return results
    if args.command == "interval":
        if args.all == bool(args.targets):
            raise ValueError("give device ids or --all, not both")
        targets = services.hub.device_ids() if args.all else args.targets
        return [services.hub.set_interval(device_id, args.ms) for device_id in targets]
    if args.command == "maintain":
        return services.hub.invoke(args.device_id, MAINTENANCE_METHOD)
    if args.command == "alerts":
        return services.data.alerts(args.days, args.device, now)
    if args.command == "active":
        return services.data.active()
    if not 0 <= args.show <= 100:
        raise ValueError("show must be between 0 and 100")
    return services.data.archive(args.hours, args.show, now)


def main(
    argv: Sequence[str] | None = None,
    *,
    config_loader: Callable[[], ServiceConfig] = load_config,
    services_factory: ServicesFactory = _default_services,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        config = config_loader()
        with services_factory(config) as services:
            result = _execute(args, services, config)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ClientAuthenticationError as exc:
        print(f"error: not signed in to Azure ({exc.message}); run 'az login'", file=sys.stderr)
        return 1
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except AzureError as exc:
        print(f"error: {exc.message or exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


def _utf8_output() -> None:
    """Emit UTF-8 when piped: Windows otherwise uses the ANSI code page, which has no ≥ or →."""
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def console() -> None:
    _utf8_output()
    raise SystemExit(main())
