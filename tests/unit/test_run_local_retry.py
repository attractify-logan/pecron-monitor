"""Focused coverage for continuous local multi-packet retries (issue #88)."""

from unittest.mock import MagicMock, call, patch

from monitor import PecronMonitor


SETTINGS_ONLY = {"battery_percentage": 75, "ac_switch_hm": True}
TELEMETRY = {
    "host_packet_data_jdb": {
        "host_packet_voltage": 52.8,
        "host_packet_temp": 24,
    }
}


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def make_monitor(make_config, model="E3800LFP", *, local=True, poll_interval=25):
    config = make_config(with_lan=local, with_auth=local)
    config["poll_interval"] = poll_interval
    config["devices"][0]["name"] = model
    monitor = PecronMonitor(config)
    monitor.devices = [
        {
            "product_key": "p11u2b",
            "device_key": "AABBCCDDEEFF",
            "device_name": model,
            "product_name": model,
            "controls": {},
        }
    ]
    if local:
        monitor.local_transports = {"AABBCCDDEEFF": object()}
    monitor.latest_data = {"AABBCCDDEEFF": dict(SETTINGS_ONLY)}
    monitor._running = True
    return monitor


def test_incomplete_eligible_local_device_retries_within_cycle(make_config):
    monitor = make_monitor(make_config)
    # A complete value in the persistent merged cache came from an older cycle
    # and must not suppress retries for this cycle's settings-only local read.
    monitor.latest_data["AABBCCDDEEFF"] = dict(TELEMETRY)
    clock = FakeClock()
    monitor._request_status = MagicMock()

    with (
        patch("monitor.time.monotonic", side_effect=clock.monotonic),
        patch("monitor.time.sleep", side_effect=clock.sleep),
    ):
        elapsed = monitor._request_status_with_local_retries(25)

    assert monitor._request_status.call_args_list == [
        call(),
        call(device_keys={"AABBCCDDEEFF"}),
        call(device_keys={"AABBCCDDEEFF"}),
        call(device_keys={"AABBCCDDEEFF"}),
    ]
    assert clock.sleeps == [10.0, 10.0, 5.0]
    assert elapsed == 25.0


def test_complete_eligible_local_data_stops_without_retry(make_config):
    monitor = make_monitor(make_config)
    clock = FakeClock()

    def complete_first_request(device_keys=None):
        monitor._local_data_keys.add("AABBCCDDEEFF")

    monitor._request_status = MagicMock(side_effect=complete_first_request)

    with (
        patch("monitor.time.monotonic", side_effect=clock.monotonic),
        patch("monitor.time.sleep", side_effect=clock.sleep),
    ):
        elapsed = monitor._request_status_with_local_retries(25)

    monitor._request_status.assert_called_once_with()
    assert clock.sleeps == []
    assert elapsed == 0.0


def test_non_multi_packet_model_remains_single_attempt(make_config):
    monitor = make_monitor(make_config, model="E1500LFP")
    clock = FakeClock()
    monitor._request_status = MagicMock()

    with (
        patch("monitor.time.monotonic", side_effect=clock.monotonic),
        patch("monitor.time.sleep", side_effect=clock.sleep),
    ):
        elapsed = monitor._request_status_with_local_retries(25)

    monitor._request_status.assert_called_once_with()
    assert clock.sleeps == []
    assert elapsed == 0.0


def test_elapsed_retry_time_never_creates_negative_or_stacked_cycle_delay(make_config):
    monitor = make_monitor(make_config, poll_interval=10)
    clock = FakeClock()
    request_count = 0

    def request_status(device_keys=None):
        nonlocal request_count
        request_count += 1
        clock.advance(6)
        if request_count == 3:
            monitor.latest_data["AABBCCDDEEFF"] = dict(TELEMETRY)
            monitor._running = False

    monitor._request_status = MagicMock(side_effect=request_status)
    monitor.authenticate = MagicMock()
    monitor.connect_mqtt = MagicMock()
    monitor._run_init_rules = MagicMock()
    monitor._token_needs_refresh = MagicMock(return_value=False)
    monitor._try_cloud_recovery = MagicMock()
    monitor._recover_mqtt_connection = MagicMock()

    with (
        patch("monitor.time.monotonic", side_effect=clock.monotonic),
        patch("monitor.time.sleep", side_effect=clock.sleep),
    ):
        monitor.run(force_offline=True)

    assert request_count == 3
    assert clock.sleeps == [3, 4.0, 4.0]
    assert all(delay >= 0 for delay in clock.sleeps)
