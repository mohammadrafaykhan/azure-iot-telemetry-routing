import json
import logging
import types
import typing
from datetime import UTC, datetime
from types import SimpleNamespace

import azure.functions as func
import pytest
from fakes import FakeStore

import function_app


def eh_event(body, device="press-01", source="Telemetry", sequence=1, metadata=None):
    return SimpleNamespace(
        get_body=lambda: body,
        iothub_metadata={"connection-device-id": device, "message-source": source},
        enqueued_time=datetime(2026, 11, 23, 12, 0, tzinfo=UTC),
        sequence_number=sequence,
        offset=str(sequence * 10),
        metadata=metadata,
    )


def test_all_three_functions_are_indexed_with_their_bindings():
    functions = {f.get_function_name(): f for f in function_app.app.get_functions()}
    assert set(functions) == {"stream", "connectivity", "presence"}
    stream = json.loads(functions["stream"].get_function_json())["bindings"][0]
    assert stream["type"] == "eventHubTrigger" and stream["cardinality"] == "MANY"
    assert stream["connection"] == "EventHubConnection" and stream["eventHubName"] == "%EVENTHUB_NAME%"
    assert functions["stream"].get_settings_dict("retry_policy")["strategy"] == "exponential_backoff"
    assert (
        json.loads(functions["connectivity"].get_function_json())["bindings"][0]["type"] == "eventGridTrigger"
    )
    timer = json.loads(functions["presence"].get_function_json())["bindings"][0]
    assert timer["schedule"] == "0 */1 * * * *"


def test_azure_sdk_http_logging_is_quiet():
    assert logging.getLogger("azure.core.pipeline.policies.http_logging_policy").getEffectiveLevel() >= (
        logging.WARNING
    )


def test_batch_binding_uses_typing_list():
    # The Python worker refuses to index list[EventHubEvent] ("invalid non-type annotation");
    # only typing.List is accepted for a cardinality-many binding.
    user_function = function_app.stream._function.get_user_function()
    hint = typing.get_type_hints(user_function)["events"]
    assert type(hint) is not types.GenericAlias
    assert typing.get_origin(hint) is list and typing.get_args(hint) == (func.EventHubEvent,)


def test_to_routed_maps_metadata_and_skips_bad_events(caplog):
    properties = [{"site": "karachi"}, {}]
    events = [
        eh_event(b'{"vibration_mm_s": 2.5}', metadata={"PropertiesArray": properties}),
        eh_event(b"not json", sequence=2),
    ]
    routed = function_app.to_routed(events)
    assert len(routed) == 1
    assert routed[0].device_id == "press-01" and routed[0].properties == {"site": "karachi"}
    assert "skipping event 2" in caplog.text


def test_properties_array_mismatch_falls_back_to_empty():
    events = [eh_event(b"{}", metadata={"PropertiesArray": json.dumps([{"a": 1}, {"b": 2}])})]
    assert function_app.to_routed(events)[0].properties == {}


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(function_app, "_store", lambda: fake)
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "st1")
    function_app._settings.cache_clear()
    yield fake
    function_app._settings.cache_clear()


def test_stream_function_processes_a_batch(store):
    function_app.stream([eh_event(b'{"vibration_mm_s": 2.5}')])
    assert len(store.blobs) == 1
    assert store.presence("press-01")[0].last_seen is not None


def test_connectivity_function_records_connection_state(store):
    event = func.EventGridEvent(
        id="1",
        data={"deviceId": "press-01", "deviceConnectionStateEventInfo": {"sequenceNumber": "0A"}},
        topic="t",
        subject="devices/press-01",
        event_type="Microsoft.Devices.DeviceDisconnected",
        event_time=datetime(2026, 11, 23, 12, 0, tzinfo=UTC),
        data_version="1",
    )
    function_app.connectivity(event)
    assert store.presence("press-01")[0].connected is False


def test_connectivity_function_skips_other_events(store, caplog):
    event = func.EventGridEvent(
        id="2",
        data={"deviceId": "press-01"},
        topic="t",
        subject="s",
        event_type="Microsoft.Devices.DeviceCreated",
        event_time=None,
        data_version="1",
    )
    function_app.connectivity(event)
    assert store.presences == {} and "skipping Event Grid event 2" in caplog.text
