import asyncio
import json
import random
from types import SimpleNamespace

import pytest
from azure.iot.device import exceptions

from device.__main__ import supervise
from device.config import DeviceConfig
from device.machine import MachineSimulator, MachineState
from device.press import MAINTENANCE_METHOD, Backoff, Press


class FakeHub:
    def __init__(self, *, connect_error=None, send_error=None, twin=None, on_send=None):
        self.connect_error, self.send_error = connect_error, send_error
        self.twin = twin or {"desired": {"$version": 1}, "reported": {}}
        self.on_send = on_send
        self.sent, self.reported, self.responses = [], [], []
        self.shut_down = False
        self.on_twin_desired_properties_patch_received = None
        self.on_method_request_received = None

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def shutdown(self):
        self.shut_down = True

    async def get_twin(self):
        return self.twin

    async def patch_twin_reported_properties(self, patch):
        self.reported.append(patch)

    async def send_message(self, message):
        if self.send_error:
            raise self.send_error
        self.sent.append(json.loads(message.data))
        if self.on_send:
            await self.on_send(self)

    async def send_method_response(self, response):
        self.responses.append(response)


class Harness:
    def __init__(self, hubs, wear_rate=1.0, interval_ms=60_000):
        self.hubs, self.used, self.pauses = list(hubs), [], []
        self.stop = asyncio.Event()
        machine = MachineSimulator(MachineState(interval_ms=interval_ms), random.Random(1), wear_rate)
        self.press = Press(
            "press-01",
            client_factory=self._client,
            machine=machine,
            backoff=Backoff(base_s=0.001, cap_s=0.001),
            rng=random.Random(2),
        )
        self.machine = machine

    def _client(self):
        hub = self.hubs.pop(0)
        self.used.append(hub)
        return hub

    async def stop_after_send(self, hub):
        self.stop.set()

    async def run(self):
        await asyncio.wait_for(self.press.run(self.stop), timeout=5)


async def test_sends_telemetry_and_reports_on_startup():
    h = Harness([])
    h.hubs.append(FakeHub(on_send=h.stop_after_send))
    await h.run()
    hub = h.used[0]
    assert hub.sent[0]["deviceId"] == "press-01" and hub.sent[0]["sequence"] == 1
    assert {"temperature_c", "vibration_mm_s", "motor_current_a", "rpm", "health"} <= set(hub.sent[0])
    assert hub.reported[0]["firmwareVersion"] == "1.0.0"
    assert hub.reported[0]["telemetryIntervalMs"] == {"value": 60000, "ac": 200, "av": 0, "ad": "default"}
    assert hub.shut_down


async def test_applies_desired_interval_from_the_twin():
    h = Harness([])
    twin = {"desired": {"$version": 3, "telemetryIntervalMs": 10000}, "reported": {}}
    h.hubs.append(FakeHub(twin=twin, on_send=h.stop_after_send))
    await h.run()
    assert h.press.interval_ms == 10000
    assert h.used[0].reported[0]["telemetryIntervalMs"] == {
        "value": 10000,
        "ac": 200,
        "av": 3,
        "ad": "applied",
    }


@pytest.mark.parametrize("value", [500, 3_600_001, "fast", True])
async def test_rejects_invalid_desired_interval(value):
    h = Harness([])

    async def patch(hub):
        await h.press.on_desired_patch({"telemetryIntervalMs": value, "$version": 4})
        h.stop.set()

    h.hubs.append(FakeHub(on_send=patch))
    await h.run()
    ack = h.used[0].reported[-1]["telemetryIntervalMs"]
    assert ack["ac"] == 400 and ack["av"] == 4 and h.press.interval_ms == 60000


async def test_interval_change_wakes_the_loop():
    h = Harness([], interval_ms=60_000)
    sends = []

    async def on_send(hub):
        sends.append(len(hub.sent))
        if len(hub.sent) == 1:
            await h.press.on_desired_patch({"telemetryIntervalMs": 1000, "$version": 2})
        else:
            h.stop.set()

    h.hubs.append(FakeHub(on_send=on_send))
    await h.run()
    assert sends == [1, 2]


@pytest.mark.parametrize(
    "error",
    [
        exceptions.ConnectionDroppedError("gone"),
        exceptions.OperationCancelled("cancelled"),
        exceptions.ClientError("Unexpected failure"),
        exceptions.CredentialError("device disabled"),
    ],
)
async def test_recovers_from_session_failures(error):
    h = Harness([])
    h.hubs += [FakeHub(send_error=error), FakeHub(on_send=h.stop_after_send)]
    await h.run()
    assert len(h.used) == 2 and all(hub.shut_down for hub in h.used)


async def test_backoff_grows_across_failed_sessions_and_resets_after_delivery(monkeypatch):
    h = Harness([], interval_ms=1000)
    attempts = []
    real = Backoff.delay

    def spy(self, attempt, rng):
        attempts.append(attempt)
        return real(self, attempt, rng)

    monkeypatch.setattr(Backoff, "delay", spy)

    async def fail_next(hub):
        hub.send_error = exceptions.ConnectionDroppedError("gone")

    drop = exceptions.ConnectionDroppedError("gone")
    refused = exceptions.ConnectionFailedError("refused")
    h.hubs += [
        FakeHub(connect_error=refused),
        FakeHub(send_error=drop),
        FakeHub(on_send=fail_next),
        FakeHub(send_error=drop),
        FakeHub(on_send=h.stop_after_send),
    ]
    h.machine.state.interval_ms = 1000
    await asyncio.wait_for(h.press.run(h.stop), timeout=10)
    assert attempts == [1, 2, 1, 2]


def test_backoff_delay_is_bounded():
    backoff, rng = Backoff(base_s=5, cap_s=300), random.Random(0)
    assert backoff.delay(1, rng) == 5
    assert all(5 <= backoff.delay(n, rng) <= 300 for n in range(1, 20))


async def test_maintenance_method_resets_wear_and_reports():
    h = Harness([], wear_rate=40)
    request = SimpleNamespace(name=MAINTENANCE_METHOD, request_id="1", payload=None)
    unknown = SimpleNamespace(name="Explode", request_id="2", payload=None)

    async def on_send(hub):
        if len(hub.sent) < 3:
            return
        await h.press.on_method_request(request)
        await h.press.on_method_request(unknown)
        h.stop.set()

    h.machine.state.interval_ms = 1000
    hub = FakeHub(on_send=on_send)
    h.hubs.append(hub)
    h.press._wait = lambda stop, seconds: asyncio.sleep(0)
    await h.run()
    assert h.machine.state.wear == 0.0
    assert [r.status for r in hub.responses] == [200, 404]
    assert "lastMaintenance" in hub.reported[-1]


def test_degraded_press_wears_much_faster():
    normal = MachineSimulator(MachineState(), random.Random(3))
    degraded = MachineSimulator(MachineState(), random.Random(3), wear_rate=40)
    for _ in range(20):
        normal.step()
        degraded.step()
    assert degraded.state.wear > 0.3 > normal.state.wear
    assert degraded.read()["vibration_mm_s"] > normal.read()["vibration_mm_s"]
    with pytest.raises(ValueError):
        MachineSimulator(MachineState(), wear_rate=-1)


async def test_supervise_contains_a_crashing_press(caplog):
    class Crashing:
        device_id = "press-03"

        async def run(self, stop):
            raise RuntimeError("boom")

    await supervise(Crashing(), asyncio.Event(), 0)
    assert "press stopped unexpectedly" in caplog.text


def test_device_discovery_needs_certificate_and_key(tmp_path):
    for name in ("press-01.cert.pem", "press-01.key.pem", "press-02.cert.pem"):
        (tmp_path / name).write_text("x")
    config = DeviceConfig("hub.azure-devices.net", tmp_path)
    assert config.discover_devices() == ["press-01"]
    assert DeviceConfig("h", tmp_path / "missing").discover_devices() == []
