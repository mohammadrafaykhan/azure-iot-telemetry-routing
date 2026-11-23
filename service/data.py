"""Read the pipeline's output: alerts and presence (Table storage) and the raw archive (Blob)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from azure.core.credentials import TokenCredential
from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient

# Written by the function app (functions/pipeline/store.py and archive.py).
ALERTS_TABLE = "alerts"
ALERT_STATE_TABLE = "alertstate"
PRESENCE_TABLE = "presence"
PRESENCE_PARTITION = "presence"
ARCHIVE_CONTAINER = "archive"
ARCHIVE_PREFIX = "raw"
MAX_DAYS = 31
MAX_HOURS = 72


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return value


def _row(entity: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: _jsonable(entity[name]) for name in fields if entity.get(name) is not None}


class Reader(Protocol):
    def alerts(self, days: int, device_id: str | None, now: datetime) -> list[dict]: ...
    def active(self) -> list[dict]: ...
    def presence(self) -> list[dict]: ...
    def archive(self, hours: int, show: int, now: datetime) -> dict: ...


class OpsData:
    def __init__(self, account_name: str, credential: TokenCredential) -> None:
        self._tables = TableServiceClient(
            endpoint=f"https://{account_name}.table.core.windows.net", credential=credential
        )
        self._blobs = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net", credential=credential
        )

    def alerts(self, days: int, device_id: str | None, now: datetime) -> list[dict]:
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f"days must be between 1 and {MAX_DAYS}")
        table = self._tables.get_table_client(ALERTS_TABLE)
        fields = ("at", "deviceId", "kind", "subject", "state", "severity", "message", "value", "threshold")
        rows: list[dict] = []
        for offset in range(days):
            day = (now - timedelta(days=offset)).strftime("%Y%m%d")
            query, parameters = "PartitionKey eq @day", {"day": day}
            if device_id:
                query += " and deviceId eq @device"
                parameters["device"] = device_id
            rows += [_row(e, fields) for e in table.query_entities(query, parameters=parameters)]
        return rows

    def active(self) -> list[dict]:
        """Threshold alerts currently held, plus devices currently offline or silent."""
        state = self._tables.get_table_client(ALERT_STATE_TABLE)
        held = [
            {
                "deviceId": e["deviceId"],
                "kind": "threshold",
                "subject": e["metric"],
                "severity": e["severity"],
                "since": _jsonable(e.get("since")),
            }
            for e in state.query_entities("severity ne @ok", parameters={"ok": "ok"})
        ]
        for row in self.presence():
            if row.get("offlineAlerted"):
                held.append({"deviceId": row["deviceId"], "kind": "offline", "severity": "critical"})
            if row.get("silentAlerted"):
                held.append({"deviceId": row["deviceId"], "kind": "silent", "severity": "warning"})
        return held

    def presence(self) -> list[dict]:
        table = self._tables.get_table_client(PRESENCE_TABLE)
        fields = (
            "deviceId",
            "connected",
            "changedAt",
            "lastSeen",
            "intervalMs",
            "offlineAlerted",
            "silentAlerted",
        )
        rows = table.query_entities("PartitionKey eq @pk", parameters={"pk": PRESENCE_PARTITION})
        return [_row(e, fields) for e in rows]

    def archive(self, hours: int, show: int, now: datetime) -> dict:
        if not 1 <= hours <= MAX_HOURS:
            raise ValueError(f"hours must be between 1 and {MAX_HOURS}")
        container = self._blobs.get_container_client(ARCHIVE_CONTAINER)
        blobs: list[dict] = []
        for offset in reversed(range(hours)):
            prefix = f"{ARCHIVE_PREFIX}/{now - timedelta(hours=offset):%Y/%m/%d/%H}/"
            blobs += [
                {"name": b.name, "bytes": b.size, "lastModified": _jsonable(b.last_modified)}
                for b in container.list_blobs(name_starts_with=prefix)
            ]
        records: list[dict] = []
        for blob in reversed(blobs):
            if len(records) >= show:
                break
            text = container.download_blob(blob["name"]).readall().decode("utf-8")
            lines = [json.loads(line) for line in text.splitlines() if line.strip()]
            records = lines[-(show - len(records)) :] + records
        return {
            "blobs": len(blobs),
            "bytes": sum(b["bytes"] for b in blobs),
            "latest": blobs[-5:],
            "records": records,
        }
