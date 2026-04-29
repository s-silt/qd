"""Unit tests for side-effect-free static methods in worker.BaseWorker.

Because importing worker.py requires the full DB + mcrypto stack (which needs
the pbkdf2 C extension), we extract the pure functions' logic verbatim and
test them in isolation.  The goal is to catch regressions in the retry-backoff
schedule and the night-time scheduling guard.

If the source of those functions ever changes, update the copies below.
"""
import datetime
import unittest
from typing import Optional


# ---------------------------------------------------------------------------
# Local copies of the two pure functions under test
# (Kept in sync with worker.py::BaseWorker.failed_count_to_time / fix_next_time)
# ---------------------------------------------------------------------------

def _failed_count_to_time(
    last_failed_count: int,
    retry_count: int = 8,
    retry_interval: Optional[int] = None,
    interval: Optional[int] = None,
) -> Optional[int]:
    """Verbatim copy of BaseWorker.failed_count_to_time (worker.py)."""
    next = None
    if last_failed_count < retry_count or retry_count == -1:
        if retry_interval:
            next = retry_interval
        else:
            if last_failed_count == 0:
                next = 10 * 60
            elif last_failed_count == 1:
                next = 110 * 60
            elif last_failed_count == 2:
                next = 4 * 60 * 60
            elif last_failed_count == 3:
                next = 6 * 60 * 60
            elif last_failed_count < retry_count or retry_count == -1:
                next = 11 * 60 * 60
            else:
                next = None
    elif retry_count == 0:
        next = None

    if next and not retry_interval:
        if interval is None:
            interval = 12 * 60 * 60
        next = min(next, interval)
    return next


def _fix_next_time(next_ts: float, gmt_offset: float) -> float:
    """Verbatim copy of BaseWorker.fix_next_time (worker.py)."""
    date = datetime.datetime.fromtimestamp(next_ts, tz=datetime.timezone.utc)
    local_date = date - datetime.timedelta(minutes=gmt_offset)
    if local_date.hour < 2:
        next_ts += 2 * 60 * 60
    if local_date.hour > 21:
        next_ts -= 3 * 60 * 60
    return next_ts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFailedCountToTime(unittest.TestCase):
    """Tests for BaseWorker.failed_count_to_time."""

    # ---- Backoff schedule (default retry_count = 8, no explicit interval) ----

    def test_first_failure_returns_10_min(self):
        # min(600, 12*3600) = 600
        self.assertEqual(_failed_count_to_time(0, retry_count=8), 10 * 60)

    def test_second_failure_returns_110_min(self):
        self.assertEqual(_failed_count_to_time(1, retry_count=8), 110 * 60)

    def test_third_failure_returns_4_hours(self):
        self.assertEqual(_failed_count_to_time(2, retry_count=8), 4 * 60 * 60)

    def test_fourth_failure_returns_6_hours(self):
        self.assertEqual(_failed_count_to_time(3, retry_count=8), 6 * 60 * 60)

    def test_fifth_to_seventh_failure_returns_11_hours(self):
        for count in (4, 5, 6, 7):
            with self.subTest(count=count):
                self.assertEqual(
                    _failed_count_to_time(count, retry_count=8),
                    11 * 60 * 60,
                )

    # ---- Exhaustion / boundary ----

    def test_at_max_retries_returns_none(self):
        # last_failed_count == retry_count  → no more retries → None
        self.assertIsNone(_failed_count_to_time(8, retry_count=8))

    def test_beyond_max_retries_returns_none(self):
        self.assertIsNone(_failed_count_to_time(10, retry_count=8))

    # ---- retry_count = 0 → no retries at all ----

    def test_zero_retry_count_always_returns_none(self):
        for count in (0, 1, 5):
            with self.subTest(count=count):
                self.assertIsNone(_failed_count_to_time(count, retry_count=0))

    # ---- retry_count = -1 → unlimited retries ----

    def test_unlimited_retries_never_returns_none(self):
        for count in (0, 10, 100):
            with self.subTest(count=count):
                self.assertIsNotNone(
                    _failed_count_to_time(count, retry_count=-1)
                )

    # ---- retry_interval overrides the backoff table ----

    def test_explicit_retry_interval_used(self):
        self.assertEqual(
            _failed_count_to_time(0, retry_count=8, retry_interval=300), 300
        )

    def test_explicit_retry_interval_not_capped_by_interval(self):
        # When retry_interval is set the `interval` cap is NOT applied.
        self.assertEqual(
            _failed_count_to_time(0, retry_count=8, retry_interval=300, interval=60),
            300,
        )

    # ---- interval caps the computed backoff ----

    def test_short_interval_caps_backoff(self):
        # First failure: 10*60 = 600 s, but interval = 120 s  →  min = 120
        self.assertEqual(_failed_count_to_time(0, retry_count=8, interval=120), 120)

    def test_long_interval_does_not_affect_short_backoff(self):
        # First failure: 600 s < 48 h  → no change
        self.assertEqual(
            _failed_count_to_time(0, retry_count=8, interval=48 * 60 * 60), 10 * 60
        )

    def test_default_interval_caps_at_12_hours(self):
        # 11-hour backoff < 12-hour default cap → returned unchanged
        self.assertEqual(
            _failed_count_to_time(4, retry_count=8, interval=None), 11 * 60 * 60
        )


class TestFixNextTime(unittest.TestCase):
    """Tests for BaseWorker.fix_next_time.

    The function adjusts `next` so that local execution falls between
    02:00 and 21:00.  We use a fixed gmt_offset to make tests
    timezone-independent.

    Convention used here:
        gmt_offset = 0    ↔  UTC (time.timezone == 0 on a UTC host)
        local_time  = UTC − (gmt_offset/60)  seconds
    So for gmt_offset=0 local == UTC.
    """

    def _utc_ts(self, hour: int, minute: int = 0) -> float:
        """Return UTC timestamp for 2025-01-15 HH:MM:00 UTC."""
        return datetime.datetime(
            2025, 1, 15, hour, minute, 0, tzinfo=datetime.timezone.utc
        ).timestamp()

    def test_hour_0_shifted_forward(self):
        # Local 00:00 (gmt_offset=0)  → +2 h
        ts = self._utc_ts(0)
        self.assertAlmostEqual(_fix_next_time(ts, 0), ts + 2 * 3600, delta=1)

    def test_hour_1_shifted_forward(self):
        # Local 01:59  → still < 2  → +2 h
        ts = self._utc_ts(1, 59)
        self.assertAlmostEqual(_fix_next_time(ts, 0), ts + 2 * 3600, delta=1)

    def test_hour_2_not_shifted(self):
        # Local 02:00  → on boundary  → no shift
        ts = self._utc_ts(2)
        self.assertAlmostEqual(_fix_next_time(ts, 0), ts, delta=1)

    def test_hour_noon_not_shifted(self):
        ts = self._utc_ts(12)
        self.assertAlmostEqual(_fix_next_time(ts, 0), ts, delta=1)

    def test_hour_21_not_shifted(self):
        # Local 21:00  → not > 21  → no shift
        ts = self._utc_ts(21)
        self.assertAlmostEqual(_fix_next_time(ts, 0), ts, delta=1)

    def test_hour_22_shifted_backward(self):
        # Local 22:00  → > 21  → -3 h
        ts = self._utc_ts(22)
        self.assertAlmostEqual(_fix_next_time(ts, 0), ts - 3 * 3600, delta=1)

    def test_hour_23_shifted_backward(self):
        ts = self._utc_ts(23, 30)
        self.assertAlmostEqual(_fix_next_time(ts, 0), ts - 3 * 3600, delta=1)

    def test_gmt_offset_shifts_local_time(self):
        # gmt_offset = 480  →  local = UTC − 480 min = UTC − 8 h
        # UTC 09:00  →  local 01:00  →  < 2  →  +2 h applied
        ts = self._utc_ts(9)
        result = _fix_next_time(ts, gmt_offset=480)
        self.assertAlmostEqual(result, ts + 2 * 3600, delta=1)

    def test_negative_gmt_offset_east_timezone(self):
        # gmt_offset = -480  →  local = UTC + 8 h  (e.g. Asia/Shanghai)
        # UTC 14:00  →  local 22:00  →  > 21  →  -3 h applied
        ts = self._utc_ts(14)
        result = _fix_next_time(ts, gmt_offset=-480)
        self.assertAlmostEqual(result, ts - 3 * 3600, delta=1)


if __name__ == "__main__":
    unittest.main()
