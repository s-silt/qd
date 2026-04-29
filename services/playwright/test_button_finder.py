"""Tests for button_finder scoring logic."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from button_finder import pick_button, score_candidate  # noqa: E402


class TestScore(unittest.TestCase):
    def test_high_priority_chinese(self):
        self.assertGreater(score_candidate("立即签到"), score_candidate("领取"))
        self.assertGreater(score_candidate("每日打卡"), score_candidate("daily"))

    def test_negative_login_button(self):
        self.assertLess(score_candidate("登录"), 0)
        self.assertLess(score_candidate("Sign Up"), 0)

    def test_hint_boost(self):
        # 即使是普通词, 命中提示词也能压过签到关键字
        with_hint = score_candidate("领取奖品", hint="领取奖品")
        no_hint = score_candidate("领取奖品")
        self.assertGreater(with_hint, no_hint)

    def test_empty(self):
        self.assertEqual(score_candidate(""), 0)
        self.assertEqual(score_candidate("   "), 0)

    def test_english_signin(self):
        self.assertGreater(score_candidate("Check In"), 0)
        self.assertGreater(score_candidate("Daily Sign In"), 0)


class TestPickButton(unittest.TestCase):
    def _cands(self, *texts):
        return [
            {"text": t, "tag": "button", "selector": f"#b{i}", "href": ""}
            for i, t in enumerate(texts)
        ]

    def test_pick_signin_among_noise(self):
        cands = self._cands("登录", "退出", "立即签到", "搜索", "首页")
        chosen, top = pick_button(cands)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["text"], "立即签到")
        self.assertLessEqual(len(top), 10)

    def test_no_signin_returns_none(self):
        cands = self._cands("登录", "退出", "搜索")
        chosen, top = pick_button(cands)
        self.assertIsNone(chosen)
        # 但仍返回排序后的候选
        self.assertEqual(len(top), 3)

    def test_hint_overrides(self):
        cands = self._cands("立即签到", "领取奖品", "登录")
        chosen, _ = pick_button(cands, hint="领取奖品")
        self.assertEqual(chosen["text"], "领取奖品")

    def test_priority_order(self):
        cands = self._cands("打卡", "立即签到")
        chosen, _ = pick_button(cands)
        # 立即签到 (high) > 打卡 (high) 但前者更具体
        self.assertEqual(chosen["text"], "立即签到")


class TestParseCookieStr(unittest.TestCase):
    """从 app.py 测试 cookie 字符串解析；如果运行时 playwright 未安装会跳过。"""

    def test_basic(self):
        try:
            from app import _parse_cookie_str_to_storage_state  # type: ignore
        except Exception:
            self.skipTest("app.py 依赖 playwright/fastapi, 跳过")
            return
        st = _parse_cookie_str_to_storage_state(
            "session=abc; token=xyz", "https://example.com/sign"
        )
        self.assertEqual(len(st["cookies"]), 2)
        names = {c["name"] for c in st["cookies"]}
        self.assertSetEqual(names, {"session", "token"})
        for c in st["cookies"]:
            self.assertEqual(c["domain"], ".example.com")
            self.assertTrue(c["secure"])

    def test_empty_pairs_ignored(self):
        try:
            from app import _parse_cookie_str_to_storage_state  # type: ignore
        except Exception:
            self.skipTest("app.py 依赖 playwright/fastapi, 跳过")
            return
        st = _parse_cookie_str_to_storage_state(
            ";; key=val ; ;= ;", "http://x.com/"
        )
        self.assertEqual(len(st["cookies"]), 1)
        self.assertEqual(st["cookies"][0]["name"], "key")


if __name__ == "__main__":
    unittest.main()
