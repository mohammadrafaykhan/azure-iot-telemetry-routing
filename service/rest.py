"""Minimal Entra ID-authenticated REST client for the IoT Hub service API.

The IoT Hub service SDK for Python depends on the deprecated uamqp, so opsctl calls
the documented REST API through azure-core, which supplies token refresh, retries
with Retry-After, and logging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol

from azure.core import PipelineClient
from azure.core.credentials import TokenCredential
from azure.core.pipeline.policies import (
    BearerTokenCredentialPolicy,
    HeadersPolicy,
    NetworkTraceLoggingPolicy,
    RetryPolicy,
    UserAgentPolicy,
)
from azure.core.rest import HttpRequest

USER_AGENT = "opsctl/0.1"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class Requester(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response: ...


def _error_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("message", "Message", "errorMessage"):
            if body.get(key):
                return str(body[key]).split("\n")[0][:300]
    return fallback


class RestClient:
    def __init__(
        self,
        endpoint: str,
        credential: TokenCredential,
        scope: str,
        api_version: str,
        **kwargs: Any,
    ) -> None:
        self._api_version = api_version
        self._client = PipelineClient(
            endpoint,
            policies=[
                HeadersPolicy({"Accept": "application/json"}),
                UserAgentPolicy(USER_AGENT),
                RetryPolicy(retry_total=4),
                BearerTokenCredentialPolicy(credential, scope),
                NetworkTraceLoggingPolicy(),
            ],
            **kwargs,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        request = HttpRequest(
            method,
            self._client.format_url(path),
            params={"api-version": self._api_version},
            headers=headers or {},
            json=body,
        )
        response = self._client.send_request(request)
        text = response.text()
        payload: Any = None
        if text:
            try:
                payload = response.json()
            except ValueError:
                payload = text
        if response.status_code >= 400:
            raise ApiError(response.status_code, _error_message(payload, response.reason or "request failed"))
        return Response(response.status_code, payload, dict(response.headers))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RestClient:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


def paginate(requester: Requester, path: str, query: dict, page_size: int, limit: int) -> list[dict]:
    """POST query pages, following x-ms-continuation, up to ``limit`` items."""
    items: list[dict] = []
    continuation: str | None = None
    while len(items) < limit:
        headers = {"x-ms-max-item-count": str(min(page_size, limit - len(items)))}
        if continuation:
            headers["x-ms-continuation"] = continuation
        response = requester.request("POST", path, body=query, headers=headers)
        items.extend(response.body or [])
        continuation = {k.lower(): v for k, v in response.headers.items()}.get("x-ms-continuation")
        if not continuation:
            break
    return items[:limit]
