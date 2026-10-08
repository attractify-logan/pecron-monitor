"""--device selection: write commands must never fan out to every configured device."""

from unittest.mock import patch

import pytest
import yaml

import pecron_monitor


CONFIG = {
    "email": "x",
    "password": "x",
    "region": "na",
    "devices": [
        {"product_key": "p11uJn", "device_key": "BC2A33E2B4BB", "name": "E3800LFP"},
        {"product_key": "p11u2b", "device_key": "682499E40D61", "name": "E1500LFP"},
    ],
}


@pytest.mark.parametrize("selector", ["682499E40D61", "682499e40d61", "e1500lfp"])
def test_select_device_matches_key_or_name_case_insensitively(selector):
    selected = pecron_monitor.select_device(CONFIG, selector)

    assert [d["device_key"] for d in selected["devices"]] == ["682499E40D61"]
    assert len(CONFIG["devices"]) == 2  # the original config is not modified


def test_select_device_rejects_unknown_and_ambiguous_selectors():
    with pytest.raises(ValueError, match="matches no configured device"):
        pecron_monitor.select_device(CONFIG, "F3000LFP")

    twins = {**CONFIG, "devices": [{**d, "name": "Van"} for d in CONFIG["devices"]]}
    with pytest.raises(ValueError, match="matches several devices"):
        pecron_monitor.select_device(twins, "van")


def _run_main(tmp_path, monkeypatch, *argv):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(CONFIG))
    monkeypatch.setattr("sys.argv", ["pecron-monitor", "--config", str(config_path), *argv])
    with patch("pecron_monitor.PecronMonitor") as monitor_cls:
        try:
            pecron_monitor.main()
        except SystemExit as e:
            return e.code, monitor_cls
    return 0, monitor_cls


@pytest.mark.parametrize(
    "command",
    [
        ("--ac", "off"),
        ("--dc", "off"),
        ("--control", "ac_switch_hm", "off"),
        ("--probe-control", "ac_output_voltage_io"),
    ],
)
def test_write_commands_with_several_devices_require_device(tmp_path, monkeypatch, command):
    code, monitor_cls = _run_main(tmp_path, monkeypatch, *command)

    assert code == 2
    monitor_cls.assert_not_called()


def test_write_command_with_device_only_reaches_that_device(tmp_path, monkeypatch):
    code, monitor_cls = _run_main(tmp_path, monkeypatch, "--ac", "off", "--device", "E3800LFP")

    assert code == 0
    config = monitor_cls.call_args.args[0]
    assert [d["device_key"] for d in config["devices"]] == ["BC2A33E2B4BB"]
    monitor_cls.return_value.one_shot_command.assert_called_once_with(
        ac=False, dc=None, force_offline=False
    )


def test_read_only_commands_still_cover_every_device(tmp_path, monkeypatch):
    code, monitor_cls = _run_main(tmp_path, monkeypatch, "--status")

    assert code == 0
    assert len(monitor_cls.call_args.args[0]["devices"]) == 2
