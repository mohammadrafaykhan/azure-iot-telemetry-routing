import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography import x509

from service.certs import create_certificate, ensure_identity, thumbprint, validate_device_id
from service.cli import Services, main
from service.config import ConfigError, ServiceConfig
from service.data import OpsData
from service.hub import HubRegistry
from service.rest import ApiError, Response

HUB = "iot-telemetry.azure-devices.net"
NOW = datetime(2026, 11, 23, 12, 30, tzinfo=UTC)


class FakeRequester:
    """Replays scripted responses keyed by (method, path) and records every call."""

    def __init__(self, routes=None):
        self.routes = {key: list(value) for key, value in (routes or {}).items()}
        self.calls = []

    def request(self, method, path, *, body=None, headers=None):
        self.calls.append((method, path, body, headers or {}))
        queue = self.routes.get((method, path))
        if not queue:
            return Response(200, {})
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --- certificates ------------------------------------------------------------------


def test_certificate_is_a_self_signed_client_leaf():
    _, cert = create_certificate("press-01", days=30)
    assert cert.subject == cert.issuer
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert len(thumbprint(cert)) == 64


@pytest.mark.parametrize("device_id", ["", "-lead", "a/b", "a?b", "x" * 129, "press 01"])
def test_device_ids_must_be_file_name_safe(device_id):
    with pytest.raises(ValueError):
        validate_device_id(device_id)


def test_ensure_identity_creates_once_then_reuses(tmp_path):
    cert, created = ensure_identity(tmp_path, "press-01")
    again, created_again = ensure_identity(tmp_path, "press-01")
    assert created and not created_again and thumbprint(cert) == thumbprint(again)
    if sys.platform != "win32":
        assert os.stat(tmp_path / "press-01.key.pem").st_mode & 0o777 == 0o600


def test_ensure_identity_refuses_a_half_identity(tmp_path):
    (tmp_path / "press-01.cert.pem").write_text("x")
    with pytest.raises(ValueError, match="only one of"):
        ensure_identity(tmp_path, "press-01")


# --- hub registry ------------------------------------------------------------------


def test_register_creates_device_with_pinned_thumbprint_and_tags():
    rest = FakeRequester({("GET", "/devices/press-01"): [ApiError(404, "not found")]})
    result = HubRegistry(rest).register("press-01", "AB" * 32, {"site": "karachi"})
    assert result["identity"] == "created"
    _, _, body, headers = rest.calls[1]
    assert body["authentication"] == {
        "type": "selfSigned",
        "x509Thumbprint": {"primaryThumbprint": "AB" * 32, "secondaryThumbprint": None},
    }
    assert "If-Match" not in headers
    assert rest.calls[2][:3] == ("PATCH", "/twins/press-01", {"tags": {"site": "karachi"}})
    assert rest.calls[2][3]["If-Match"] == "*"


def test_register_is_idempotent_and_updates_a_changed_thumbprint():
    existing = {
        "deviceId": "press-01",
        "etag": "MQ==",
        "authentication": {"type": "selfSigned", "x509Thumbprint": {"primaryThumbprint": "AA"}},
    }
    rest = FakeRequester({("GET", "/devices/press-01"): [Response(200, existing), Response(200, existing)]})
    registry = HubRegistry(rest)
    assert registry.register("press-01", "AA", {})["identity"] == "unchanged"
    assert registry.register("press-01", "BB", {})["identity"] == "updated"
    method, _, body, headers = rest.calls[-1]
    assert method == "PUT" and headers["If-Match"] == '"MQ=="'
    assert body["authentication"]["x509Thumbprint"]["primaryThumbprint"] == "BB"


def test_register_propagates_unexpected_errors():
    rest = FakeRequester({("GET", "/devices/press-01"): [ApiError(403, "forbidden")]})
    with pytest.raises(ApiError):
        HubRegistry(rest).register("press-01", "AA", {})


def test_list_devices_takes_connection_state_from_the_identity():
    twin = {
        "deviceId": "press-01",
        "status": "enabled",
        "connectionState": "Disconnected",
        "tags": {"site": "karachi"},
        "properties": {"reported": {"telemetryIntervalMs": {"value": 10000}, "lastMaintenance": "T"}},
    }
    rest = FakeRequester(
        {
            ("POST", "/devices/query"): [Response(200, [twin])],
            ("GET", "/devices/press-01"): [Response(200, {"connectionState": "Connected"})],
        }
    )
    (device,) = HubRegistry(rest).list_devices()
    assert device["connectionState"] == "Connected"
    assert device["telemetryIntervalMs"] == 10000 and device["lastMaintenance"] == "T"


def test_set_interval_and_invoke_method():
    twin = {"properties": {"desired": {"telemetryIntervalMs": 10000, "$version": 5}}}
    rest = FakeRequester(
        {
            ("PATCH", "/twins/press-01"): [Response(200, twin)],
            ("POST", "/twins/press-01/methods"): [Response(200, {"status": 200, "payload": {"wear": 0.0}})],
        }
    )
    registry = HubRegistry(rest)
    assert registry.set_interval("press-01", 10000) == {
        "deviceId": "press-01",
        "desired": 10000,
        "version": 5,
    }
    result = registry.invoke("press-01", "CompleteMaintenance")
    assert result["status"] == 200 and result["payload"] == {"wear": 0.0}
    assert rest.calls[-1][2]["methodName"] == "CompleteMaintenance"


# --- CLI ---------------------------------------------------------------------------


class FakeReader:
    def __init__(self):
        self.calls = []

    def alerts(self, days, device_id, now):
        self.calls.append(("alerts", days, device_id))
        return [{"deviceId": "press-03", "state": "raised"}]

    def active(self):
        return [{"deviceId": "press-03", "kind": "threshold"}]

    def presence(self):
        return [{"deviceId": "press-01", "connected": True}]

    def archive(self, hours, show, now):
        self.calls.append(("archive", hours, show))
        return {"blobs": 2, "records": []}


def run_cli(argv, tmp_path, rest=None, reader=None):
    rest = rest or FakeRequester()
    reader = reader or FakeReader()

    @contextmanager
    def factory(config):
        yield Services(hub=HubRegistry(rest), data=reader)

    config = ServiceConfig(HUB, "st1", tmp_path / "certs")
    code = main(argv, config_loader=lambda: config, services_factory=factory)
    return code, rest, reader


def test_cli_devices_add_creates_certificates_and_registers(tmp_path, capsys):
    rest = FakeRequester({("GET", "/devices/press-01"): [ApiError(404, "nf")]})
    code, rest, _ = run_cli(["devices", "add", "press-01", "--tag", "site=karachi"], tmp_path, rest)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out[0]["certificate"] == "created" and out[0]["identity"] == "created"
    assert (tmp_path / "certs" / "press-01.key.pem").exists()


def test_cli_devices_list_joins_presence(tmp_path, capsys):
    rest = FakeRequester(
        {
            ("POST", "/devices/query"): [Response(200, [{"deviceId": "press-01"}])],
            ("GET", "/devices/press-01"): [Response(200, {"connectionState": "Connected"})],
        }
    )
    code, _, _ = run_cli(["devices", "list"], tmp_path, rest)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out[0]["presence"] == {"deviceId": "press-01", "connected": True}


def test_cli_interval_for_all_devices(tmp_path, capsys):
    rest = FakeRequester(
        {("POST", "/devices/query"): [Response(200, [{"deviceId": "press-01"}, {"deviceId": "press-02"}])]}
    )
    code, rest, _ = run_cli(["interval", "--all", "--ms", "10000"], tmp_path, rest)
    assert code == 0
    assert [c[1] for c in rest.calls if c[0] == "PATCH"] == ["/twins/press-01", "/twins/press-02"]


@pytest.mark.parametrize(
    "argv",
    [
        ["interval", "--ms", "10000"],
        ["interval", "press-01", "--all", "--ms", "10000"],
        ["archive", "--show", "500"],
    ],
)
def test_cli_invalid_combinations_exit_2(tmp_path, argv, capsys):
    code, _, _ = run_cli(argv, tmp_path)
    assert code == 2 and "error:" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["interval", "press-01", "--ms", "10"], ["maintain", "bad/id"]])
def test_cli_argument_validation_exits_2(tmp_path, argv):
    with pytest.raises(SystemExit) as exc:
        run_cli(argv, tmp_path)
    assert exc.value.code == 2


def test_cli_alerts_and_archive_pass_options(tmp_path, capsys):
    _, _, reader = run_cli(["alerts", "--days", "2", "--device", "press-03"], tmp_path)
    run_cli(["archive", "--hours", "3", "--show", "5"], tmp_path, reader=reader)
    assert reader.calls == [("alerts", 2, "press-03"), ("archive", 3, 5)]


def test_cli_maintain_reports_device_offline_as_failure(tmp_path, capsys):
    rest = FakeRequester({("POST", "/twins/press-01/methods"): [ApiError(404, "DeviceNotOnline")]})
    code, _, _ = run_cli(["maintain", "press-01"], tmp_path, rest)
    assert code == 1 and "DeviceNotOnline" in capsys.readouterr().err


def test_cli_missing_configuration_exits_2(capsys):
    def missing():
        raise ConfigError("IOTHUB_HOSTNAME is not set")

    assert main(["active"], config_loader=missing) == 2
    assert "IOTHUB_HOSTNAME" in capsys.readouterr().err


# --- data reader -------------------------------------------------------------------


class FakeTable:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def query_entities(self, query, parameters):
        self.queries.append((query, parameters))
        return [row for row in self.rows if all(row.get(k) == v for k, v in _filters(query, parameters))]


def _filters(query, parameters):
    mapping = {"day": "PartitionKey", "pk": "PartitionKey", "device": "deviceId"}
    return [(mapping[name], value) for name, value in parameters.items() if name in mapping]


class FakeContainer:
    def __init__(self, blobs):
        self.blobs = blobs

    def list_blobs(self, name_starts_with):
        return [
            SimpleNamespace(name=n, size=len(d), last_modified=NOW)
            for n, d in sorted(self.blobs.items())
            if n.startswith(name_starts_with)
        ]

    def download_blob(self, name):
        return SimpleNamespace(readall=lambda: self.blobs[name])


def reader_with(tables=None, blobs=None):
    data = OpsData.__new__(OpsData)
    tables = tables or {}
    data._tables = SimpleNamespace(get_table_client=lambda name: tables[name])
    data._blobs = SimpleNamespace(get_container_client=lambda name: FakeContainer(blobs or {}))
    return data


def test_reader_alerts_query_each_day_and_filter_by_device():
    rows = [
        {"PartitionKey": "20261123", "deviceId": "press-03", "at": NOW, "state": "raised"},
        {"PartitionKey": "20261122", "deviceId": "press-01", "at": NOW, "state": "raised"},
    ]
    table = FakeTable(rows)
    data = reader_with({"alerts": table})
    assert len(data.alerts(2, None, NOW)) == 2
    assert data.alerts(1, "press-03", NOW) == [
        {"at": "2026-11-23T12:30:00Z", "deviceId": "press-03", "state": "raised"}
    ]
    with pytest.raises(ValueError):
        data.alerts(0, None, NOW)


def test_reader_active_combines_rule_state_and_presence():
    state = FakeTable(
        [
            {
                "PartitionKey": "press-03",
                "deviceId": "press-03",
                "metric": "vibration_mm_s",
                "severity": "critical",
                "since": NOW,
            }
        ]
    )
    presence = FakeTable([{"PartitionKey": "presence", "deviceId": "press-05", "offlineAlerted": True}])
    active = reader_with({"alertstate": state, "presence": presence}).active()
    assert [(a["deviceId"], a["kind"]) for a in active] == [
        ("press-03", "threshold"),
        ("press-05", "offline"),
    ]


def test_reader_archive_lists_hours_and_shows_latest_records():
    blobs = {
        "raw/2026/11/23/11/a.jsonl": b'{"n": 1}\n{"n": 2}\n',
        "raw/2026/11/23/12/b.jsonl": b'{"n": 3}\n',
        "raw/2026/11/22/12/old.jsonl": b'{"n": 0}\n',
    }
    result = reader_with(blobs=blobs).archive(2, 2, NOW)
    assert result["blobs"] == 2 and result["records"] == [{"n": 2}, {"n": 3}]
    assert reader_with(blobs=blobs).archive(1, 0, NOW)["records"] == []
    with pytest.raises(ValueError):
        reader_with().archive(100, 1, NOW)


def test_piped_output_is_utf8_even_on_an_ansi_code_page():
    # Alert messages contain ≥ and →, which crashed `opsctl alerts | ...` on Windows (cp1252).
    code = "from service.cli import _utf8_output; _utf8_output(); print('4.7 ≥ 4.5 (ok → warning)')"
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, check=True)
    assert result.stdout.decode("utf-8").strip() == "4.7 ≥ 4.5 (ok → warning)"
