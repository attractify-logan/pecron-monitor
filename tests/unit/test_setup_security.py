"""Tests for setup wizard credential handling."""

import os
from unittest.mock import patch

import pytest

import setup_wizard


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_secure_config_permissions_sets_owner_only(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("password: secret\n")
    os.chmod(config_path, 0o644)

    setup_wizard._secure_config_permissions(config_path)

    assert config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_warn_if_existing_config_is_readable(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("password: secret\n")
    os.chmod(config_path, 0o644)

    setup_wizard._warn_if_config_is_readable(config_path)

    captured = capsys.readouterr()
    assert "readable by group/other users" in captured.out
    assert "chmod 600" in captured.out


def test_pecron_password_prompt_uses_getpass(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "CONFIG_PATH", tmp_path / "config.yaml")

    with (
        patch("builtins.input", side_effect=["user@example.com", "na"]),
        patch("setup_wizard.getpass.getpass", return_value="secret") as mock_getpass,
        patch("setup_wizard.login", side_effect=RuntimeError("stop after prompt")),
    ):
        setup_wizard.setup_wizard(auto=False)

    mock_getpass.assert_called_once_with("Pecron account password: ")
