import io
import time

import pytest
import requests
import urllib3
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import RequestsTransport

from service.rest import ApiError, Response, RestClient, paginate


class StaticToken:
    def get_token(self, *scopes, **kwargs):
        return AccessToken("token", int(time.time()) + 3600)


class RecordingAdapter(requests.adapters.BaseAdapter):
    """Stands in for the network under azure-core's real RequestsTransport."""

    def __init__(self, status, body):
        super().__init__()
        self.status, self.body, self.sent = status, body, []

    def send(self, request, **kwargs):
        self.sent.append(request)
        headers = {"Content-Type": "application/json", "Content-Length": str(len(self.body))}
        raw = requests.Response()
        raw.status_code, raw.url, raw.request = self.status, request.url, request
        raw.headers.update(headers)
        raw.raw = urllib3.HTTPResponse(
            body=io.BytesIO(self.body), status=self.status, headers=headers, preload_content=False
        )
        return raw

    def close(self):
        pass


def recording_transport(status=200, body=b'{"ok": true}'):
    adapter = RecordingAdapter(status, body)
    session = requests.Session()
    session.mount("https://", adapter)
    return RequestsTransport(session=session, session_owner=False), adapter


def test_rest_client_sends_authenticated_https_requests_to_the_endpoint():
    transport, adapter = recording_transport()
    with RestClient(
        "https://dps.example.net", StaticToken(), "scope/.default", "2021-10-01", transport=transport
    ) as client:
        response = client.request(
            "PUT", "/enrollmentGroups/presses", body={"a": 1}, headers={"If-Match": "e"}
        )
    sent = adapter.sent[0]
    assert sent.url == "https://dps.example.net/enrollmentGroups/presses?api-version=2021-10-01"
    assert sent.headers["Authorization"] == "Bearer token"
    assert sent.headers["If-Match"] == "e"
    assert response.status == 200 and response.body == {"ok": True}


def test_rest_client_raises_api_error_with_the_service_message():
    transport, _ = recording_transport(status=404, body=b'{"message": "Enrollment not found"}')
    client = RestClient("https://dps.example.net", StaticToken(), "s", "v", transport=transport)
    with client, pytest.raises(ApiError, match="404: Enrollment not found"):
        client.request("GET", "/enrollments/x")


class Pages:
    def __init__(self):
        self.calls = []

    def request(self, method, path, *, body=None, headers=None):
        self.calls.append(headers)
        if "x-ms-continuation" not in headers:
            return Response(200, [{"n": 1}, {"n": 2}], {"x-ms-continuation": "page2"})
        return Response(200, [{"n": 3}], {})


def test_paginate_follows_continuation_until_limit():
    pages = Pages()
    assert paginate(pages, "/q", {}, 2, 10) == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert pages.calls[1]["x-ms-continuation"] == "page2"
    assert paginate(Pages(), "/q", {}, 2, 1) == [{"n": 1}]
