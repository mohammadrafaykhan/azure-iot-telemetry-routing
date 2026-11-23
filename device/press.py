"""One simulated press: connect, report, send telemetry, and recover from failures.

The application owns reconnection (``connection_retry=False`` on the client), so
every failure ends the session and the press reconnects after a backoff that grows
while sessions keep failing before any telemetry is delivered. A press never gives
up: a hub over its daily quota, a disabled identity, or a network outage all heal
eventually, and the presence alerts in the cloud report the gap meanwhile.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from azure.iot.device import MethodResponse, exceptions

from device.machine import MAX_INTERVAL_MS, MIN_INTERVAL_MS, MachineSimulator
from device.telemetry import build_payload, to_message

FIRMWARE_VERSION = "1.0.0"
MAINTENANCE_METHOD = "CompleteMaintenance"

# Failures the press recovers from by reconnecting. Seen live in this portfolio: a
# dropped connection cancels in-flight sends (OperationCancelled), and a hub over its
# daily quota answers 403, which the SDK raises as a generic ClientError.
RECOVERABLE: tuple[type[Exception], ...] = (
    exceptions.ConnectionFailedError,
    exceptions.ConnectionDroppedError,
    exceptions.OperationTimeout,
    exceptions.OperationCancelled,
    exceptions.NoConnectionError,
    exceptions.ServiceError,
    exceptions.ClientError,
)

logger = logging.getLogger("press")


class HubClient(Protocol):
    on_twin_desired_properties_patch_received: Any
    on_method_request_received: Any

    async def connect(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def send_message(self, message: Any) -> None: ...
    async def get_twin(self) -> dict: ...
    async def patch_twin_reported_properties(self, patch: dict) -> None: ...
    async def send_method_response(self, response: Any) -> None: ...


@dataclass(frozen=True)
class Backoff:
    base_s: float = 5.0
    cap_s: float = 300.0

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Exponential with jitter in [50%, 100%] of the step, never below ``base_s``."""
        step = min(self.cap_s, self.base_s * (2 ** max(0, attempt - 1)))
        return max(self.base_s, step * rng.uniform(0.5, 1.0))


DEFAULT_BACKOFF = Backoff()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ack(value: Any, code: int, version: int, description: str) -> dict:
    return {"value": value, "ac": code, "av": version, "ad": description}


def _describe(error: BaseException) -> str:
    cause = error.__cause__
    return f"{type(error).__name__}: {cause or error}"


class Press:
    def __init__(
        self,
        device_id: str,
        *,
        client_factory: Callable[[], HubClient],
        machine: MachineSimulator,
        backoff: Backoff = DEFAULT_BACKOFF,
        rng: random.Random | None = None,
    ) -> None:
        self.device_id = device_id
        self._client_factory = client_factory
        self._machine = machine
        self._backoff = backoff
        self._rng = rng or random.Random()
        self._log = logging.LoggerAdapter(logger, {"device": device_id})
        self._client: HubClient | None = None
        self._sequence = 0
        self._delivered = False
        self._wake = asyncio.Event()

    @property
    def interval_ms(self) -> int:
        return self._machine.state.interval_ms

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            client = self._client_factory()
            self._client = client
            self._delivered = False
            reason = ""
            try:
                await client.connect()
                self._log.info("connected")
                await self._session(client, stop)
            except exceptions.CredentialError as exc:
                reason = f"hub rejected the identity ({_describe(exc)})"
            except RECOVERABLE as exc:
                reason = f"session ended ({_describe(exc)})"
            finally:
                self._client = None
                with contextlib.suppress(Exception):
                    await client.shutdown()
            if stop.is_set():
                return
            failures = 1 if self._delivered else failures + 1
            delay = self._backoff.delay(failures, self._rng)
            self._log.warning("%s; reconnecting in %.1fs", reason, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _session(self, client: HubClient, stop: asyncio.Event) -> None:
        client.on_twin_desired_properties_patch_received = self.on_desired_patch
        client.on_method_request_received = self.on_method_request
        twin = await client.get_twin()
        await client.patch_twin_reported_properties(self._startup_report(twin))
        while not stop.is_set():
            self._machine.step()
            self._sequence += 1
            payload = build_payload(
                self.device_id, self._sequence, self._machine.read(), self._machine.health()
            )
            await client.send_message(to_message(payload))
            self._delivered = True
            await self._wait(stop, self.interval_ms / 1000)

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep for the interval, cut short by stop or by an interval change."""
        self._wake.clear()
        waiters = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._wake.wait())]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    def _startup_report(self, twin: dict) -> dict:
        report: dict[str, Any] = {"firmwareVersion": FIRMWARE_VERSION, "connectivity": "online"}
        desired = twin.get("desired") or {}
        if "telemetryIntervalMs" in desired:
            report["telemetryIntervalMs"] = self._apply_interval(
                desired["telemetryIntervalMs"], int(desired.get("$version", 0))
            )
        else:
            report["telemetryIntervalMs"] = _ack(self.interval_ms, 200, 0, "default")
        return report

    def _apply_interval(self, value: Any, version: int) -> dict:
        valid = isinstance(value, int) and not isinstance(value, bool)
        if not valid or not MIN_INTERVAL_MS <= value <= MAX_INTERVAL_MS:
            self._log.warning("rejected telemetryIntervalMs=%r", value)
            return _ack(
                self.interval_ms, 400, version, f"must be an integer {MIN_INTERVAL_MS}-{MAX_INTERVAL_MS}"
            )
        self._machine.state.interval_ms = value
        self._wake.set()
        self._log.info("telemetry interval set to %d ms", value)
        return _ack(value, 200, version, "applied")

    async def on_desired_patch(self, patch: dict) -> None:
        if "telemetryIntervalMs" not in patch:
            return
        ack = self._apply_interval(patch["telemetryIntervalMs"], int(patch.get("$version", 0)))
        await self._report({"telemetryIntervalMs": ack})

    async def on_method_request(self, request: Any) -> None:
        if request.name == MAINTENANCE_METHOD:
            self._machine.reset_wear()
            completed = _now()
            self._log.info("maintenance completed; wear reset")
            status, payload = 200, {"wear": 0.0, "completedAt": completed}
            await self._report({"lastMaintenance": completed})
        else:
            status, payload = 404, {"error": f"unknown method {request.name!r}"}
        client = self._client
        if client is not None:
            with contextlib.suppress(*RECOVERABLE):
                await client.send_method_response(
                    MethodResponse.create_from_method_request(request, status, payload)
                )

    async def _report(self, patch: dict) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.patch_twin_reported_properties(patch)
        except RECOVERABLE as exc:
            self._log.warning("could not report %s (%s)", sorted(patch), _describe(exc))
