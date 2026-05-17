"""Tests for domain fallback logic in cloud_api.login() (#31)."""

import base64
import json
import time
from unittest.mock import patch

import pytest

from cloud_api import _DOMAIN_RETRY_CODES, login
from constants import REGIONS


def _make_region(with_fallback: bool = True) -> dict:
    """Build a minimal region config for login fallback tests."""
    region = {
        "name": "Test Region",
        "base_url": "https://test-api.example.com",
        "mqtt_host": "test-mqtt.example.com",
        "mqtt_port": 8443,
        "mqtt_path": "/ws/v2",
        "user_domain": "C.DM.TEST.1",
        "user_domain_secret": "PrimarySecretABC123",
    }
    if with_fallback:
        region["user_domain_fallback"] = "U.DM.TEST.1"
        region["user_domain_secret_fallback"] = "FallbackSecretXYZ789"
    return region


def _fake_jwt_token(uid: str = "test-uid") -> str:
    """Create a fake JWT-like token for testing."""
    header = base64.b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    payload = (
        base64.b64encode(json.dumps({"uid": uid, "exp": int(time.time()) + 7200}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.fakesig"


class TestDomainFallback:
    """Tests for the login() domain fallback mechanism."""

    @patch("cloud_api._do_login")
    def test_primary_succeeds_no_fallback_attempted(self, mock_do_login):
        mock_do_login.return_value = {"token": "t", "uid": "u", "expires_at": 0}
        region = _make_region(with_fallback=True)

        result = login("test@test.com", "pass", region)

        assert result["token"] == "t"
        mock_do_login.assert_called_once()
        call_region = mock_do_login.call_args[0][2]
        assert call_region["user_domain"] == "C.DM.TEST.1"

    @patch("cloud_api._do_login")
    def test_fallback_on_domain_not_exist(self, mock_do_login):
        mock_do_login.side_effect = [
            RuntimeError("Login failed (code 5015): UserDomain does not exist"),
            {"token": "fallback-token", "uid": "u", "expires_at": 0},
        ]
        region = _make_region(with_fallback=True)

        result = login("test@test.com", "pass", region)

        assert result["token"] == "fallback-token"
        assert mock_do_login.call_count == 2
        fallback_region = mock_do_login.call_args_list[1][0][2]
        assert fallback_region["user_domain"] == "U.DM.TEST.1"
        assert fallback_region["user_domain_secret"] == "FallbackSecretXYZ789"

    @patch("cloud_api._do_login")
    def test_fallback_on_email_not_registered(self, mock_do_login):
        mock_do_login.side_effect = [
            RuntimeError("Login failed (code 5031): Email address is not registered"),
            {"token": "fb", "uid": "u", "expires_at": 0},
        ]
        region = _make_region(with_fallback=True)

        result = login("test@test.com", "pass", region)

        assert result["token"] == "fb"
        assert mock_do_login.call_count == 2

    @patch("cloud_api._do_login")
    def test_fallback_on_signature_failed(self, mock_do_login):
        mock_do_login.side_effect = [
            RuntimeError("Login failed (code 5420): Signature verification failed"),
            {"token": "fb", "uid": "u", "expires_at": 0},
        ]
        region = _make_region(with_fallback=True)

        result = login("test@test.com", "pass", region)

        assert result["token"] == "fb"
        assert mock_do_login.call_count == 2

    @patch("cloud_api._do_login")
    def test_no_fallback_on_wrong_password(self, mock_do_login):
        mock_do_login.side_effect = RuntimeError(
            "Login failed (code 5353): Password decryption failed"
        )
        region = _make_region(with_fallback=True)

        with pytest.raises(RuntimeError, match="5353"):
            login("test@test.com", "pass", region)

        mock_do_login.assert_called_once()

    @patch("cloud_api._do_login")
    def test_no_fallback_when_not_configured(self, mock_do_login):
        mock_do_login.side_effect = RuntimeError(
            "Login failed (code 5015): UserDomain does not exist"
        )
        region = _make_region(with_fallback=False)

        with pytest.raises(RuntimeError, match="5015"):
            login("test@test.com", "pass", region)

        mock_do_login.assert_called_once()

    @patch("cloud_api._do_login")
    def test_both_domains_fail_raises_fallback_error(self, mock_do_login):
        mock_do_login.side_effect = [
            RuntimeError("Login failed (code 5015): UserDomain does not exist"),
            RuntimeError("Login failed (code 5031): Email not registered on fallback either"),
        ]
        region = _make_region(with_fallback=True)

        with pytest.raises(RuntimeError, match="5031"):
            login("test@test.com", "pass", region)

        assert mock_do_login.call_count == 2

    def test_na_region_has_fallback_configured(self):
        na = REGIONS["na"]
        assert "user_domain_fallback" in na
        assert "user_domain_secret_fallback" in na
        assert na["user_domain"] != na["user_domain_fallback"]

    def test_eu_region_has_no_fallback(self):
        eu = REGIONS["eu"]
        assert "user_domain_fallback" not in eu

    def test_retry_codes_include_known_errors(self):
        assert 5015 in _DOMAIN_RETRY_CODES
        assert 5031 in _DOMAIN_RETRY_CODES
        assert 5420 in _DOMAIN_RETRY_CODES


class TestLoginResponseParsing:
    @patch("cloud_api.urllib.request.urlopen")
    def test_login_error_includes_code(self, mock_urlopen):
        response = mock_urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"code": 5015, "msg": "bad domain"})

        with pytest.raises(RuntimeError, match=r"code 5015"):
            login("test@test.com", "pass", _make_region(with_fallback=False))

    @patch("cloud_api.urllib.request.urlopen")
    def test_login_success_parses_jwt_payload(self, mock_urlopen):
        response = mock_urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps(
            {"code": 200, "data": {"accessToken": {"token": _fake_jwt_token("uid-1")}}}
        )

        result = login("test@test.com", "pass", _make_region(with_fallback=False))

        assert result["uid"] == "uid-1"
        assert result["token"].endswith(".fakesig")
