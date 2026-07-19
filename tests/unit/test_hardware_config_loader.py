import json

import pytest

from tests.hardware.config_loader import load_hardware_devices


def test_load_preserves_optional_device_fields(monkeypatch, capsys):
    auth_key = "secret-auth-key-must-not-be-printed"
    device = {
        "device_key": "  E3800-DEVICE  ",
        "auth_key": f"  {auth_key}  ",
        "lan_ip": "  192.0.2.10  ",
        "model": "E3800LFP",
        "product_key": "secret-product-key",
        "controls": {"dc_output": {"id": 321, "type": "bool"}},
    }
    monkeypatch.setenv("PECRON_HARDWARE_JSON", json.dumps([device]))
    monkeypatch.delenv("PECRON_HARDWARE_CONFIG", raising=False)

    loaded = load_hardware_devices()

    assert loaded == [
        {
            "device_key": "E3800-DEVICE",
            "auth_key": auth_key,
            "lan_ip": "192.0.2.10",
            "model": "E3800LFP",
            "product_key": "secret-product-key",
            "controls": {"dc_output": {"id": 321, "type": "bool"}},
        }
    ]
    captured = capsys.readouterr()
    assert auth_key not in captured.out
    assert auth_key not in captured.err


@pytest.mark.parametrize("missing_field", ["device_key", "auth_key", "lan_ip"])
def test_load_requires_each_required_field(monkeypatch, missing_field):
    device = {
        "device_key": "E3800-DEVICE",
        "auth_key": "secret-auth-key",
        "lan_ip": "192.0.2.10",
    }
    del device[missing_field]
    monkeypatch.setenv("PECRON_HARDWARE_JSON", json.dumps([device]))
    monkeypatch.delenv("PECRON_HARDWARE_CONFIG", raising=False)

    with pytest.raises(ValueError, match=rf"missing required field\(s\): {missing_field}"):
        load_hardware_devices()


def test_invalid_config_does_not_print_auth_key(monkeypatch, capsys):
    auth_key = "secret-auth-key-must-stay-private"
    monkeypatch.setenv(
        "PECRON_HARDWARE_JSON",
        json.dumps([{"device_key": "E3800-DEVICE", "auth_key": auth_key}]),
    )
    monkeypatch.delenv("PECRON_HARDWARE_CONFIG", raising=False)

    with pytest.raises(ValueError, match="missing required field"):
        load_hardware_devices()

    captured = capsys.readouterr()
    assert auth_key not in captured.out
    assert auth_key not in captured.err
