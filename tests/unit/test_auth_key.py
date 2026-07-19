"""Regression tests for cloud auth-key retrieval."""

import json
import urllib.request

import pytest

from cloud_api import get_auth_key


BASE_URL = "https://example.invalid"
REGION = {"base_url": BASE_URL}
TOKEN = "test-authorization-token"
PK = "product key/+"
DK = "device key="


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self._body


def test_get_auth_key_first_endpoint_preserves_key_and_wire_contract(monkeypatch):
    auth_key = "+/8="
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"code": 200, "data": {"authKey": auth_key}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert get_auth_key(TOKEN, REGION, PK, DK) == auth_key
    assert len(requests) == 1

    request, timeout = requests[0]
    assert request.full_url == f"{BASE_URL}/v2/binding/enduserapi/getAuthKey"
    assert request.data == b"pk=product+key%2F%2B&dk=device+key%3D"
    assert request.get_header("Authorization") == TOKEN
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert timeout == 15


@pytest.mark.parametrize("first_failure", ["non_200", "exception"])
def test_get_auth_key_falls_back_to_regeneration(monkeypatch, first_failure):
    regenerated_key = "AQIDBAUGBwgJCgsMDQ4PEA=="
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        if len(urls) == 1:
            if first_failure == "exception":
                raise OSError("getAuthKey unavailable")
            return FakeResponse({"code": 403, "msg": "getAuthKey denied"})
        return FakeResponse({"code": 200, "data": {"authKey": regenerated_key}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert get_auth_key(TOKEN, REGION, PK, DK) == regenerated_key
    assert urls == [
        f"{BASE_URL}/v2/binding/enduserapi/getAuthKey",
        f"{BASE_URL}/v2/binding/enduserapi/regenerateAuthKey",
    ]


def test_get_auth_key_reports_last_failure_when_both_endpoints_fail(monkeypatch):
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        if len(urls) == 1:
            return FakeResponse({"code": 401, "msg": "initial lookup denied"})
        raise OSError("regeneration unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as error:
        get_auth_key(TOKEN, REGION, PK, DK)

    assert str(error.value) == "Failed to get authKey: regeneration unavailable"
    assert urls == [
        f"{BASE_URL}/v2/binding/enduserapi/getAuthKey",
        f"{BASE_URL}/v2/binding/enduserapi/regenerateAuthKey",
    ]
