"""Persistence: Azure Table storage for state and alerts, Blob storage for the raw archive.

Tables
* ``alertstate``  PartitionKey = device, RowKey = metric: rule state between batches.
* ``presence``    PartitionKey = "presence", RowKey = device: connection and last-seen.
* ``alerts``      PartitionKey = UTC day (yyyymmdd), RowKey = inverted time + identity,
  so a day's alerts list newest first and a re-delivered alert overwrites itself.

Presence rows are written by three functions, so writes use the row's ETag and a
concurrent change surfaces as :class:`Conflict` for the caller to re-read and retry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from itertools import groupby
from typing import Any, Protocol

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.storage.blob import BlobServiceClient, ContentSettings

from pipeline.model import Alert, utc
from pipeline.presence import Presence
from pipeline.rules import RuleState

ALERT_STATE_TABLE = "alertstate"
ALERTS_TABLE = "alerts"
PRESENCE_TABLE = "presence"
PRESENCE_PARTITION = "presence"
ARCHIVE_CONTAINER = "archive"
MAX_TICKS_MS = 10**15
TRANSACTION_LIMIT = 100


class Conflict(Exception):
    """The row changed since it was read."""


class Store(Protocol):
    def rule_states(self, device_id: str) -> dict[str, RuleState]: ...
    def save_rule_states(self, device_id: str, states: Mapping[str, RuleState]) -> None: ...
    def presence(self, device_id: str) -> tuple[Presence, str | None]: ...
    def all_presence(self) -> list[tuple[Presence, str | None]]: ...
    def save_presence(self, presence: Presence, etag: str | None) -> None: ...
    def add_alerts(self, alerts: Iterable[Alert]) -> None: ...
    def archive(self, name: str, data: bytes) -> None: ...


_KEY_ESCAPES = {"%": "%25", "/": "%2F", "\\": "%5C", "#": "%23", "?": "%3F"}


def table_key(value: str) -> str:
    """Escape the characters Table storage forbids in keys (IoT Hub IDs may contain '?')."""
    return "".join(_KEY_ESCAPES.get(char, char) for char in value)


def _without_none(entity: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entity.items() if value is not None}


def _time(value: Any) -> datetime | None:
    return utc(value) if isinstance(value, datetime) else None


def rule_state_entity(device_id: str, metric: str, state: RuleState) -> dict[str, Any]:
    return _without_none(
        {
            "PartitionKey": table_key(device_id),
            "RowKey": table_key(metric),
            "metric": metric,
            "deviceId": device_id,
            "severity": state.severity,
            "pending": state.pending,
            "pendingCount": state.pending_count,
            "lastAt": state.last_at,
            "since": state.since,
        }
    )


def rule_state_from(entity: Mapping[str, Any]) -> RuleState:
    return RuleState(
        severity=str(entity.get("severity", "ok")),
        pending=str(entity.get("pending", "ok")),
        pending_count=int(entity.get("pendingCount", 0)),
        last_at=_time(entity.get("lastAt")),
        since=_time(entity.get("since")),
    )


def presence_entity(presence: Presence) -> dict[str, Any]:
    return _without_none(
        {
            "PartitionKey": PRESENCE_PARTITION,
            "RowKey": table_key(presence.device_id),
            "deviceId": presence.device_id,
            "connected": presence.connected,
            "sequence": presence.sequence or None,
            "changedAt": presence.changed_at,
            "lastSeen": presence.last_seen,
            "intervalMs": presence.interval_ms,
            "offlineAlerted": presence.offline_alerted,
            "silentAlerted": presence.silent_alerted,
        }
    )


def presence_from(entity: Mapping[str, Any]) -> Presence:
    connected = entity.get("connected")
    interval = entity.get("intervalMs")
    return Presence(
        device_id=str(entity["deviceId"]),
        connected=bool(connected) if connected is not None else None,
        sequence=str(entity.get("sequence", "")),
        changed_at=_time(entity.get("changedAt")),
        last_seen=_time(entity.get("lastSeen")),
        interval_ms=int(interval) if interval is not None else None,
        offline_alerted=bool(entity.get("offlineAlerted", False)),
        silent_alerted=bool(entity.get("silentAlerted", False)),
    )


def alert_entity(alert: Alert) -> dict[str, Any]:
    at = utc(alert.at)
    inverted = MAX_TICKS_MS - int(at.timestamp() * 1000)
    return _without_none(
        {
            "PartitionKey": at.strftime("%Y%m%d"),
            "RowKey": table_key(f"{inverted:015d}_{alert.device_id}_{alert.kind}_{alert.subject}"),
            "deviceId": alert.device_id,
            "kind": alert.kind,
            "subject": alert.subject,
            "state": alert.state,
            "severity": alert.severity,
            "message": alert.message,
            "at": at,
            "value": alert.value,
            "threshold": alert.threshold,
        }
    )


class AzureStore:
    """Store backed by one storage account, authenticated with a token credential."""

    def __init__(self, account_name: str, credential: TokenCredential) -> None:
        self._tables = TableServiceClient(
            endpoint=f"https://{account_name}.table.core.windows.net", credential=credential
        )
        self._blobs = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net", credential=credential
        )

    def rule_states(self, device_id: str) -> dict[str, RuleState]:
        table = self._tables.get_table_client(ALERT_STATE_TABLE)
        rows = table.query_entities("PartitionKey eq @pk", parameters={"pk": table_key(device_id)})
        return {str(row["metric"]): rule_state_from(row) for row in rows}

    def save_rule_states(self, device_id: str, states: Mapping[str, RuleState]) -> None:
        if not states:
            return
        table = self._tables.get_table_client(ALERT_STATE_TABLE)
        operations = [
            ("upsert", rule_state_entity(device_id, metric, state), {"mode": UpdateMode.REPLACE})
            for metric, state in states.items()
        ]
        for start in range(0, len(operations), TRANSACTION_LIMIT):
            table.submit_transaction(operations[start : start + TRANSACTION_LIMIT])

    def presence(self, device_id: str) -> tuple[Presence, str | None]:
        table = self._tables.get_table_client(PRESENCE_TABLE)
        try:
            row = table.get_entity(PRESENCE_PARTITION, table_key(device_id))
        except ResourceNotFoundError:
            return Presence(device_id), None
        return presence_from(row), row.metadata["etag"]

    def all_presence(self) -> list[tuple[Presence, str | None]]:
        table = self._tables.get_table_client(PRESENCE_TABLE)
        rows = table.query_entities("PartitionKey eq @pk", parameters={"pk": PRESENCE_PARTITION})
        return [(presence_from(row), row.metadata["etag"]) for row in rows]

    def save_presence(self, presence: Presence, etag: str | None) -> None:
        table = self._tables.get_table_client(PRESENCE_TABLE)
        entity = presence_entity(presence)
        try:
            if etag is None:
                table.create_entity(entity)
            else:
                table.update_entity(
                    entity,
                    mode=UpdateMode.REPLACE,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
        except (ResourceExistsError, ResourceModifiedError) as exc:
            raise Conflict(presence.device_id) from exc

    def add_alerts(self, alerts: Iterable[Alert]) -> None:
        entities = sorted((alert_entity(a) for a in alerts), key=lambda e: e["PartitionKey"])
        if not entities:
            return
        table = self._tables.get_table_client(ALERTS_TABLE)
        for _, group in groupby(entities, key=lambda e: e["PartitionKey"]):
            operations = [("upsert", entity, {"mode": UpdateMode.REPLACE}) for entity in group]
            for start in range(0, len(operations), TRANSACTION_LIMIT):
                table.submit_transaction(operations[start : start + TRANSACTION_LIMIT])

    def archive(self, name: str, data: bytes) -> None:
        self._blobs.get_blob_client(ARCHIVE_CONTAINER, name).upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/x-ndjson"),
        )
