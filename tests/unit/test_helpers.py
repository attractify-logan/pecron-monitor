"""
Unit tests for helpers.py — pure-function tests for the data normalization,
formatting, and safe dict-navigation utilities.

These functions are load-bearing: _get_kv feeds SENSOR_FIELDS lookup, _truthy
normalizes switch states from every transport, and _fmt_dhm handles remain_time
which has historically produced bogus values (see CHANGELOG v0.5.4, v0.5.5).
"""

import pytest

from helpers import _truthy, _fmt_dhm, _get_kv, _get_kv_single


# ---------------------------------------------------------------------------
# _truthy — coerces device values (0/1, '0'/'1', 'on'/'off', etc.) to bool
# ---------------------------------------------------------------------------


class TestTruthy:
    @pytest.mark.parametrize(
        "value,expected",
        [
            # None passthrough
            (None, None),
            # Native bools
            (True, True),
            (False, False),
            # Numbers
            (0, False),
            (1, True),
            (42, True),
            (-1, True),
            (0.0, False),
            (0.5, True),
            # Truthy strings
            ("1", True),
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("t", True),
            ("yes", True),
            ("y", True),
            ("on", True),
            ("open", True),
            ("enabled", True),
            # Falsy strings
            ("0", False),
            ("false", False),
            ("False", False),
            ("f", False),
            ("no", False),
            ("n", False),
            ("off", False),
            ("close", False),
            ("closed", False),
            ("disabled", False),
            ("", False),
            # Whitespace handling
            ("  on  ", True),
            ("  off  ", False),
            # Unknown strings default to True (matches current helper behavior)
            ("weird", True),
        ],
    )
    def test_coercion(self, value, expected):
        assert _truthy(value) == expected

    def test_truthy_falls_back_to_bool_for_other_types(self):
        assert _truthy([]) is False
        assert _truthy([1]) is True
        assert _truthy({}) is False
        assert _truthy({"k": "v"}) is True


# ---------------------------------------------------------------------------
# _fmt_dhm — format minutes as human-readable string
# ---------------------------------------------------------------------------


class TestFmtDhm:
    @pytest.mark.parametrize(
        "minutes,expected",
        [
            # Sub-day values
            (0, "0h00m"),
            (5, "0h05m"),
            (60, "1h00m"),
            (75, "1h15m"),
            (522, "8h42m"),  # matches README example output (8h 42m)
            (1439, "23h59m"),
            # Day-or-more values
            (1440, "1d00h00m"),
            (1500, "1d01h00m"),
            (2880, "2d00h00m"),
            ("60", "1h00m"),  # string coercion
        ],
    )
    def test_valid(self, minutes, expected):
        assert _fmt_dhm(minutes) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "not-a-number",
            [1, 2, 3],
            {},
            -1,  # negative — guarded (CHANGELOG v0.5.4)
            -100,
            65535,  # sentinel for "unreliable" — guarded
            65536,
            100000,
        ],
    )
    def test_invalid_returns_none(self, bad):
        assert _fmt_dhm(bad) is None

    def test_float_coerced_to_int(self):
        # int(75.9) == 75
        assert _fmt_dhm(75.9) == "1h15m"


# ---------------------------------------------------------------------------
# _get_kv_single — single-path nested navigation
# ---------------------------------------------------------------------------


class TestGetKvSingle:
    def test_top_level(self):
        assert _get_kv_single({"a": 1}, ("a",)) == 1

    def test_nested(self):
        assert _get_kv_single({"a": {"b": {"c": 42}}}, ("a", "b", "c")) == 42

    def test_missing_key_returns_none(self):
        assert _get_kv_single({"a": 1}, ("b",)) is None

    def test_partial_path_returns_none(self):
        assert _get_kv_single({"a": {"b": 1}}, ("a", "c")) is None

    def test_traverses_through_none(self):
        assert _get_kv_single({"a": None}, ("a", "b")) is None

    def test_non_dict_in_path(self):
        # Can't descend into an int
        assert _get_kv_single({"a": 5}, ("a", "b")) is None

    def test_zero_value_returned_as_is(self):
        # 0 and False are legitimate values — must not be coerced to None
        assert _get_kv_single({"a": 0}, ("a",)) == 0
        assert _get_kv_single({"a": False}, ("a",)) is False

    def test_empty_string_returned_as_is(self):
        assert _get_kv_single({"a": ""}, ("a",)) == ""


# ---------------------------------------------------------------------------
# _get_kv — multi-path lookup with fallbacks (mirrors SENSOR_FIELDS layout)
# ---------------------------------------------------------------------------


class TestGetKv:
    def test_single_path_hit(self):
        assert _get_kv({"a": 1}, ("a",)) == 1

    def test_single_path_miss_returns_default(self):
        assert _get_kv({"a": 1}, ("b",)) is None
        assert _get_kv({"a": 1}, ("b",), default="fallback") == "fallback"

    def test_multi_path_first_wins(self):
        kv = {"a": 1, "b": 2}
        assert _get_kv(kv, [("a",), ("b",)]) == 1

    def test_multi_path_falls_through(self):
        # E1500LFP shape: nested
        kv = {"host_packet_data_jdb": {"host_packet_electric_percentage": 73}}
        paths = [
            ("host_packet_data_jdb", "host_packet_electric_percentage"),
            ("battery_percentage",),
        ]
        assert _get_kv(kv, paths) == 73

    def test_multi_path_second_path_hit(self):
        # E300LFP shape: top-level
        kv = {"battery_percentage": 88}
        paths = [
            ("host_packet_data_jdb", "host_packet_electric_percentage"),
            ("battery_percentage",),
        ]
        assert _get_kv(kv, paths) == 88

    def test_multi_path_all_miss(self):
        kv = {"other": 1}
        paths = [("a",), ("b",), ("c",)]
        assert _get_kv(kv, paths) is None
        assert _get_kv(kv, paths, default=-1) == -1

    def test_empty_paths_returns_default(self):
        assert _get_kv({"a": 1}, [], default="x") == "x"
        assert _get_kv({"a": 1}, None, default="x") == "x"

    def test_zero_value_is_not_fallback_target(self):
        # Known subtlety: _get_kv returns the first NON-None result.
        # A legitimate 0 should be returned, not skipped.
        # NOTE: current implementation returns 0 correctly for single-path,
        # but for multi-path lists, 0 is ALSO treated as "hit" (result is not None).
        kv = {"a": 0, "b": 1}
        assert _get_kv(kv, [("a",), ("b",)]) == 0
