"""IoT Hub registry, twin and direct-method operations over REST with Entra ID."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from service.rest import ApiError, Requester, paginate

HUB_SCOPE = "https://iothubs.azure.net/.default"
HUB_API_VERSION = "2021-04-12"
METHOD_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 15


def _id(device_id: str) -> str:
    return quote(device_id, safe="")


def _reported_interval(twin: dict) -> Any:
    value = ((twin.get("properties") or {}).get("reported") or {}).get("telemetryIntervalMs")
    return value.get("value") if isinstance(value, dict) else value


class HubRegistry:
    def __init__(self, requester: Requester) -> None:
        self._rest = requester

    def register(self, device_id: str, thumbprint: str, tags: dict[str, str]) -> dict:
        """Create the device with a pinned certificate thumbprint, or update it to this one."""
        path = f"/devices/{_id(device_id)}"
        identity = {
            "deviceId": device_id,
            "status": "enabled",
            "authentication": {
                "type": "selfSigned",
                "x509Thumbprint": {"primaryThumbprint": thumbprint, "secondaryThumbprint": None},
            },
        }
        try:
            existing = self._rest.request("GET", path).body
        except ApiError as exc:
            if exc.status != 404:
                raise
            existing = None
        if existing is None:
            self._rest.request("PUT", path, body=identity)
            action = "created"
        elif (existing.get("authentication") or {}).get("x509Thumbprint", {}).get(
            "primaryThumbprint"
        ) == thumbprint:
            action = "unchanged"
        else:
            body = {**existing, "authentication": identity["authentication"]}
            self._rest.request("PUT", path, body=body, headers={"If-Match": f'"{existing["etag"]}"'})
            action = "updated"
        if tags:
            self._rest.request(
                "PATCH", f"/twins/{_id(device_id)}", body={"tags": tags}, headers={"If-Match": "*"}
            )
        return {"deviceId": device_id, "identity": action, "thumbprint": thumbprint, "tags": tags}

    def list_devices(self, limit: int = 100) -> list[dict]:
        """Twins plus live connection state from each identity (query results lag behind it)."""
        twins = paginate(self._rest, "/devices/query", {"query": "SELECT * FROM devices"}, 100, limit)
        devices = []
        for twin in twins:
            device_id = twin.get("deviceId", "")
            identity = self._rest.request("GET", f"/devices/{_id(device_id)}").body or {}
            reported = (twin.get("properties") or {}).get("reported") or {}
            devices.append(
                {
                    "deviceId": device_id,
                    "status": twin.get("status"),
                    "connectionState": identity.get("connectionState"),
                    "tags": twin.get("tags") or {},
                    "telemetryIntervalMs": _reported_interval(twin),
                    "lastMaintenance": reported.get("lastMaintenance"),
                }
            )
        return devices

    def device_ids(self) -> list[str]:
        twins = paginate(self._rest, "/devices/query", {"query": "SELECT deviceId FROM devices"}, 100, 1000)
        return [twin["deviceId"] for twin in twins if twin.get("deviceId")]

    def set_interval(self, device_id: str, interval_ms: int) -> dict:
        body = {"properties": {"desired": {"telemetryIntervalMs": interval_ms}}}
        twin = self._rest.request(
            "PATCH", f"/twins/{_id(device_id)}", body=body, headers={"If-Match": "*"}
        ).body
        desired = ((twin or {}).get("properties") or {}).get("desired") or {}
        return {
            "deviceId": device_id,
            "desired": desired.get("telemetryIntervalMs"),
            "version": desired.get("$version"),
        }

    def invoke(self, device_id: str, method: str, payload: Any = None) -> dict:
        body = {
            "methodName": method,
            "payload": payload,
            "responseTimeoutInSeconds": METHOD_TIMEOUT_S,
            "connectTimeoutInSeconds": CONNECT_TIMEOUT_S,
        }
        result = self._rest.request("POST", f"/twins/{_id(device_id)}/methods", body=body).body or {}
        return {
            "deviceId": device_id,
            "method": method,
            "status": result.get("status"),
            "payload": result.get("payload"),
        }
