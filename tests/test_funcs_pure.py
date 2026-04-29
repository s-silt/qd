"""Unit tests for pure helpers in libs/funcs.py and services/playwright/button_finder.py.

Cal.cal_next_ts tests use a local copy of the function to avoid importing
libs.funcs (which pulls in aiohttp).  croniter-dependent tests are skipped
gracefully when the package is not installed.

Keep _cal_next_ts in sync with libs/funcs.py::Cal.cal_next_ts.
"""
import datetime
import random
import time
import unittest
from typing import Any, Dict, Optional

try:
    import croniter as _croniter_mod  # noqa: F401
    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False


# ---------------------------------------------------------------------------
# Local copy of Cal.cal_next_ts (libs/funcs.py)
# ---------------------------------------------------------------------------

def _cal_next_ts(envs: Dict[str, Any], *, _now: Optional[datetime.datetime] = None) -> dict:
    """Verbatim copy of Cal.cal_next_ts with an injectable 'now' for cron tests."""
    r: Dict[str, Any] = {"r": "True"}
    try:
        if envs["mode"] == "ontime":
            t = f"{envs['date']} {envs['time']}"
        elif envs["mode"] == "cron":
            import croniter
            now = _now or datetime.datetime.now()
            cron = croniter.croniter(envs["cron_val"], now)
            t = cron.get_next(datetime.datetime).strftime("%Y-%m-%d %H:%M:%S")
        else:
            raise Exception("参数错误")

        d = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S").timetuple()
        ts = int(time.mktime(d))

        if "randsw" in envs:
            if envs["sw"] and envs["randsw"]:
                r_ts = random.randint(int(envs["tz1"]), int(envs["tz2"]))
                ts = ts + r_ts

        if "cron_sec" in envs:
            r_ts = 0 if envs["cron_sec"] == "" else int(envs["cron_sec"])
            ts = ts + r_ts

        r["ts"] = ts
    except Exception as e:
        r["r"] = e
    return r


# ---------------------------------------------------------------------------
# Tests for Cal.cal_next_ts (ontime mode — no croniter dependency)
# ---------------------------------------------------------------------------

class TestCalNextTsOntime(unittest.TestCase):
    """Ontime-mode scheduling tests (no croniter required)."""

    def test_ontime_returns_correct_timestamp(self):
        envs = {"mode": "ontime", "sw": True, "date": "2025-06-15", "time": "08:30:00"}
        r = _cal_next_ts(envs)
        self.assertEqual(r.get("r"), "True")
        self.assertIn("ts", r)
        expected = int(time.mktime(datetime.datetime(2025, 6, 15, 8, 30, 0).timetuple()))
        self.assertEqual(r["ts"], expected)

    def test_ontime_random_offset_applied(self):
        envs = {
            "mode": "ontime",
            "sw": True,
            "randsw": True,
            "tz1": "0",
            "tz2": "60",
            "date": "2025-06-15",
            "time": "08:30:00",
        }
        base_ts = int(time.mktime(datetime.datetime(2025, 6, 15, 8, 30, 0).timetuple()))
        r = _cal_next_ts(envs)
        self.assertEqual(r.get("r"), "True")
        self.assertGreaterEqual(r["ts"], base_ts)
        self.assertLessEqual(r["ts"], base_ts + 60)

    def test_ontime_cron_sec_offset_applied(self):
        envs = {
            "mode": "ontime",
            "sw": True,
            "cron_sec": "30",
            "date": "2025-06-15",
            "time": "08:30:00",
        }
        base_ts = int(time.mktime(datetime.datetime(2025, 6, 15, 8, 30, 0).timetuple()))
        r = _cal_next_ts(envs)
        self.assertEqual(r["ts"], base_ts + 30)

    def test_ontime_empty_cron_sec_no_offset(self):
        envs = {
            "mode": "ontime",
            "sw": True,
            "cron_sec": "",
            "date": "2025-06-15",
            "time": "08:30:00",
        }
        base_ts = int(time.mktime(datetime.datetime(2025, 6, 15, 8, 30, 0).timetuple()))
        r = _cal_next_ts(envs)
        self.assertEqual(r["ts"], base_ts)

    def test_invalid_mode_returns_error(self):
        envs = {"mode": "bad_mode", "sw": True}
        r = _cal_next_ts(envs)
        self.assertNotEqual(r.get("r"), "True")
        self.assertIsInstance(r.get("r"), Exception)

    def test_missing_date_field_returns_error(self):
        # 'date' key missing for ontime mode → KeyError
        envs = {"mode": "ontime", "sw": True, "time": "08:30:00"}
        r = _cal_next_ts(envs)
        self.assertNotEqual(r.get("r"), "True")


@unittest.skipUnless(_HAS_CRONITER, "croniter not installed")
class TestCalNextTsCron(unittest.TestCase):
    """Cron-mode scheduling tests (requires croniter)."""

    def test_cron_returns_next_timestamp(self):
        fake_now = datetime.datetime(2025, 1, 15, 10, 0, 0)
        envs = {"mode": "cron", "sw": True, "cron_val": "0 11 * * *"}
        r = _cal_next_ts(envs, _now=fake_now)
        expected = int(time.mktime(datetime.datetime(2025, 1, 15, 11, 0, 0).timetuple()))
        self.assertEqual(r.get("r"), "True")
        self.assertEqual(r["ts"], expected)

    def test_cron_daily_midnight(self):
        fake_now = datetime.datetime(2025, 3, 10, 23, 59, 0)
        envs = {"mode": "cron", "sw": True, "cron_val": "0 0 * * *"}
        r = _cal_next_ts(envs, _now=fake_now)
        expected = int(time.mktime(datetime.datetime(2025, 3, 11, 0, 0, 0).timetuple()))
        self.assertEqual(r.get("r"), "True")
        self.assertEqual(r["ts"], expected)

    def test_invalid_cron_expression_returns_error(self):
        fake_now = datetime.datetime(2025, 1, 15, 10, 0, 0)
        envs = {"mode": "cron", "sw": True, "cron_val": "not a cron"}
        r = _cal_next_ts(envs, _now=fake_now)
        self.assertNotEqual(r.get("r"), "True")


# ---------------------------------------------------------------------------
# Tests for button_finder (no heavy deps, can import directly)
# ---------------------------------------------------------------------------

class TestButtonFinderScore(unittest.TestCase):
    """Tests for services/playwright/button_finder.score_candidate."""

    @classmethod
    def _score(cls, text, hint=""):
        from services.playwright.button_finder import score_candidate
        return score_candidate(text, hint)

    def test_empty_text_scores_zero(self):
        self.assertEqual(self._score(""), 0)

    def test_high_keyword_increases_score(self):
        # "签到" is in KEYWORDS_HIGH → +20
        self.assertGreater(self._score("每日签到"), 0)

    def test_negative_keyword_decreases_score(self):
        s_positive = self._score("签到")
        s_negative = self._score("签到 登录")
        self.assertGreater(s_positive, s_negative)

    def test_hint_match_adds_bonus(self):
        s_no_hint = self._score("签到")
        s_with_hint = self._score("签到", hint="签到")
        self.assertGreater(s_with_hint, s_no_hint)

    def test_pure_negative_keyword_is_negative_or_zero(self):
        # "login" alone should score <= 0
        self.assertLessEqual(self._score("login"), 0)

    def test_medium_keyword_adds_score(self):
        # "daily" is in KEYWORDS_MEDIUM → +5
        self.assertGreater(self._score("daily"), 0)


class TestPickButton(unittest.TestCase):
    """Tests for services/playwright/button_finder.pick_button."""

    def _pick(self, candidates, hint=""):
        from services.playwright.button_finder import pick_button
        return pick_button(candidates, hint=hint)

    def test_returns_none_when_all_scores_zero_or_negative(self):
        candidates = [
            {"text": "Login", "selector": "#login", "tag": "button", "quality": "stable", "href": ""},
        ]
        chosen, top = self._pick(candidates)
        self.assertIsNone(chosen)
        self.assertEqual(len(top), 1)

    def test_returns_best_candidate(self):
        candidates = [
            {"text": "签到", "selector": "#sign", "tag": "button", "quality": "stable", "href": ""},
            {"text": "Login", "selector": "#login", "tag": "button", "quality": "stable", "href": ""},
        ]
        chosen, _ = self._pick(candidates)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["text"], "签到")

    def test_hint_influences_selection(self):
        candidates = [
            {"text": "领取奖励", "selector": "#a", "tag": "button", "quality": "stable", "href": ""},
            {"text": "每日签到", "selector": "#b", "tag": "button", "quality": "stable", "href": ""},
        ]
        # With hint "签到" the "签到" button should win
        chosen, _ = self._pick(candidates, hint="签到")
        self.assertEqual(chosen["selector"], "#b")

    def test_empty_candidates_returns_none(self):
        chosen, top = self._pick([])
        self.assertIsNone(chosen)
        self.assertEqual(top, [])


if __name__ == "__main__":
    unittest.main()
