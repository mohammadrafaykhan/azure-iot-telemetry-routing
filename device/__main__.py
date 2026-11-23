"""Run simulated presses: ``python -m device --all`` or ``python -m device press-01 press-02``.

``--degrade press-03`` makes that press wear quickly, so it crosses the vibration and
temperature thresholds within minutes and exercises the alert pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import signal

from azure.iot.device import X509
from azure.iot.device.aio import IoTHubDeviceClient

from device.config import ConfigError, DeviceConfig, load_config
from device.machine import MachineSimulator, MachineState
from device.press import Press

logger = logging.getLogger("press.runner")
STAGGER_S = 3.0
DEGRADED_WEAR_RATE = 40.0


class _DeviceField(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.device = getattr(record, "device", "-")
        return True


def build_press(config: DeviceConfig, device_id: str, wear_rate: float) -> Press:
    cert, key = config.cert_file(device_id), config.key_file(device_id)
    if not cert.exists() or not key.exists():
        raise ConfigError(f"{device_id}: missing {cert} or {key} (run 'opsctl devices add {device_id}')")
    x509 = X509(cert_file=str(cert), key_file=str(key))

    def client() -> IoTHubDeviceClient:
        return IoTHubDeviceClient.create_from_x509_certificate(
            x509=x509, hostname=config.hub_hostname, device_id=device_id, connection_retry=False
        )

    return Press(
        device_id, client_factory=client, machine=MachineSimulator(MachineState(), wear_rate=wear_rate)
    )


async def supervise(press: Press, stop: asyncio.Event, delay: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)
    try:
        await press.run(stop)
    except Exception:  # one press failing must not stop the others
        logger.exception("press stopped unexpectedly", extra={"device": press.device_id})


async def run(device_ids: list[str], all_devices: bool, degraded: set[str]) -> None:
    config = load_config()
    ids = config.discover_devices() if all_devices else device_ids
    if not ids:
        raise ConfigError(f"no device certificates found in {config.certs_dir}")
    unknown = degraded - set(ids)
    if unknown:
        raise ConfigError(f"--degrade names devices that are not running: {', '.join(sorted(unknown))}")
    presses = [
        build_press(config, device_id, DEGRADED_WEAR_RATE if device_id in degraded else 1.0)
        for device_id in ids
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    rng = random.Random()
    logger.info("starting %d press(es): %s", len(presses), ", ".join(ids))
    tasks = [asyncio.create_task(supervise(p, stop, rng.uniform(0, STAGGER_S))) for p in presses]
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="device", description="Run simulated presses.")
    parser.add_argument("device_ids", nargs="*", help="device ids with certificates in CERTS_DIR")
    parser.add_argument("--all", action="store_true", help="run every device found in CERTS_DIR")
    parser.add_argument("--degrade", action="append", default=[], metavar="ID", help="wear this press fast")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not args.device_ids and not args.all:
        parser.error("give device ids or --all")

    handler = logging.StreamHandler()
    handler.addFilter(_DeviceField())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(device)-9s %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=[handler])
    logging.getLogger("azure").setLevel(logging.ERROR)
    try:
        asyncio.run(run(args.device_ids, args.all, set(args.degrade)))
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        logger.info("interrupted")


if __name__ == "__main__":
    main()
