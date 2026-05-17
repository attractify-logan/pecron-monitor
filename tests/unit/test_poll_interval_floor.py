"""Tests for the poll_interval rate-limit floor (#29).

Pecron's cloud rate-limits per account at ~1280 polls/day. The monitor refuses
to start below MIN_POLL_INTERVAL and warns below RECOMMENDED_POLL_INTERVAL.
"""

import logging

import pytest

from monitor import (
    MIN_POLL_INTERVAL,
    RECOMMENDED_POLL_INTERVAL,
    _validate_poll_interval,
    _validate_poll_interval_for_mode,
)


class TestPollIntervalFloor:
    def test_below_min_raises_with_actionable_message(self):
        with pytest.raises(ValueError) as exc:
            _validate_poll_interval(30)
        msg = str(exc.value)
        assert "30s" in msg
        assert f"{MIN_POLL_INTERVAL}s floor" in msg
        assert "issue #29" in msg
        assert str(RECOMMENDED_POLL_INTERVAL) in msg

    def test_at_min_minus_one_raises(self):
        with pytest.raises(ValueError):
            _validate_poll_interval(MIN_POLL_INTERVAL - 1)

    def test_old_default_60_raises(self):
        # The pre-fix default was 60, which trips 4026 daily for at least
        # one EU-region account. Upgraders with `poll_interval: 60` in config
        # should hit a hard error pointing at the fix, not a quiet warning.
        with pytest.raises(ValueError) as exc:
            _validate_poll_interval(60)
        assert "60s is below the 63s floor" in str(exc.value)

    def test_at_min_warns_but_does_not_raise(self, caplog):
        caplog.set_level(logging.WARNING, logger="pecron")
        _validate_poll_interval(MIN_POLL_INTERVAL)
        assert any("below the recommended" in r.message for r in caplog.records)

    def test_below_recommended_warns(self, caplog):
        caplog.set_level(logging.WARNING, logger="pecron")
        _validate_poll_interval(RECOMMENDED_POLL_INTERVAL - 1)
        assert any("issue #29" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_at_recommended_is_silent(self, caplog):
        caplog.set_level(logging.WARNING, logger="pecron")
        _validate_poll_interval(RECOMMENDED_POLL_INTERVAL)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_above_recommended_is_silent(self, caplog):
        caplog.set_level(logging.WARNING, logger="pecron")
        _validate_poll_interval(120)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_min_is_below_recommended(self):
        # Sanity: the constants form a coherent floor/recommended pair.
        assert MIN_POLL_INTERVAL < RECOMMENDED_POLL_INTERVAL

    def test_local_mode_allows_fast_polling(self, caplog):
        caplog.set_level(logging.INFO, logger="pecron")
        _validate_poll_interval_for_mode(15, force_offline=True)
        assert any("allowed in local/offline mode" in r.message for r in caplog.records)

    def test_cloud_mode_keeps_floor(self):
        with pytest.raises(ValueError):
            _validate_poll_interval_for_mode(15, force_offline=False)


class TestPollIntervalConstants:
    def test_min_matches_brucehoult_empirical_floor(self):
        # Bruce's empirical data (#29): poll_interval=63 makes it through the
        # 23:00 UTC window, poll_interval=62 trips at 23:45 UTC. The argument
        # for 63 as the floor: at 60, his account exhausts the daily budget
        # at ~23:00 UTC; at 63, the same budget stretches to 23*63/60 = 24.15h,
        # crossing the 00:00 UTC reset.
        assert MIN_POLL_INTERVAL == 63

    def test_recommended_gives_safe_margin(self):
        # 24h * 3600s / 70s = ~1234 polls/day, well under the ~1280 cap.
        polls_per_day = (24 * 3600) // RECOMMENDED_POLL_INTERVAL
        assert polls_per_day < 1280
