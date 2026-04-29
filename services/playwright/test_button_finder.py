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

    def test_quality_field_passes_through(self):
        # 候选保留 quality 字段, 让 UI 提示稳定性
        cands = [
            {"text": "立即签到", "tag": "button", "selector": "[data-testid=\"sign\"]",
             "quality": "stable", "href": ""},
            {"text": "打卡", "tag": "a", "selector": "body > div:nth-of-type(3) > a:nth-of-type(1)",
             "quality": "fragile", "href": ""},
        ]
        chosen, top = pick_button(cands)
        self.assertEqual(chosen["text"], "立即签到")
        self.assertEqual(chosen["quality"], "stable")
        # top 列表保留 quality
        self.assertIn("quality", top[0])


class TestJSCandidatesScript(unittest.TestCase):
    """对 JS_FIND_CANDIDATES 文本做静态检查, 确保关键改动不被无意删除。"""

    def test_uses_data_testid_first(self):
        from button_finder import JS_FIND_CANDIDATES
        # data-testid 必须在 id 之前匹配
        idx_testid = JS_FIND_CANDIDATES.find("data-testid")
        idx_id = JS_FIND_CANDIDATES.find("el.id")
        self.assertGreater(idx_testid, 0)
        self.assertLess(idx_testid, idx_id)

    def test_max_depth_4(self):
        from button_finder import JS_FIND_CANDIDATES
        self.assertIn("MAX_DEPTH = 4", JS_FIND_CANDIDATES)

    def test_emits_quality_field(self):
        from button_finder import JS_FIND_CANDIDATES
        self.assertIn("quality", JS_FIND_CANDIDATES)
        self.assertIn("'stable'", JS_FIND_CANDIDATES)
        self.assertIn("'fragile'", JS_FIND_CANDIDATES)


class TestParseCookieStr(unittest.TestCase):
    def test_basic(self):
        from security import parse_cookie_str_to_storage_state
        st = parse_cookie_str_to_storage_state(
            "session=abc; token=xyz", "https://example.com/sign"
        )
        self.assertEqual(len(st["cookies"]), 2)
        names = {c["name"] for c in st["cookies"]}
        self.assertSetEqual(names, {"session", "token"})
        for c in st["cookies"]:
            self.assertEqual(c["domain"], ".example.com")
            self.assertTrue(c["secure"])

    def test_empty_pairs_ignored(self):
        from security import parse_cookie_str_to_storage_state
        st = parse_cookie_str_to_storage_state(
            ";; key=val ; ;= ;", "http://x.com/"
        )
        self.assertEqual(len(st["cookies"]), 1)
        self.assertEqual(st["cookies"][0]["name"], "key")


class TestSanitizeStorageState(unittest.TestCase):
    """剔除跨域 cookie / origin。"""

    def test_drops_unrelated_cookies(self):
        from security import sanitize_storage_state
        state = {
            "cookies": [
                {"name": "a", "value": "1", "domain": ".example.com"},
                {"name": "b", "value": "2", "domain": ".attacker.com"},
                # sub.example.com cookie 不应匹配父域 example.com
                {"name": "c", "value": "3", "domain": "sub.example.com"},
            ],
            "origins": [],
        }
        out = sanitize_storage_state(state, "https://example.com/sign")
        names = {c["name"] for c in out["cookies"]}
        self.assertSetEqual(names, {"a"})

    def test_parent_cookie_applies_to_subdomain(self):
        from security import sanitize_storage_state
        # .example.com 应匹配 api.example.com 请求 (浏览器一致行为)
        state = {
            "cookies": [{"name": "a", "value": "1", "domain": ".example.com"}],
            "origins": [],
        }
        out = sanitize_storage_state(state, "https://api.example.com/")
        self.assertEqual(len(out["cookies"]), 1)

    def test_drops_unrelated_origins(self):
        from security import sanitize_storage_state
        state = {
            "cookies": [],
            "origins": [
                {"origin": "https://example.com", "localStorage": []},
                {"origin": "https://evil.com", "localStorage": []},
            ],
        }
        out = sanitize_storage_state(state, "https://example.com/")
        self.assertEqual(len(out["origins"]), 1)
        self.assertEqual(out["origins"][0]["origin"], "https://example.com")

    def test_subdomain_match(self):
        from security import sanitize_storage_state
        # sub.example.com 请求, cookie domain=.example.com 应保留
        state = {"cookies": [{"name": "a", "value": "1", "domain": ".example.com"}], "origins": []}
        out = sanitize_storage_state(state, "https://sub.example.com/")
        self.assertEqual(len(out["cookies"]), 1)

    def test_no_match_url(self):
        from security import sanitize_storage_state
        # URL 没有 hostname 时不放任何 cookie
        state = {"cookies": [{"name": "a", "value": "1", "domain": ".example.com"}], "origins": []}
        out = sanitize_storage_state(state, "file:///etc/passwd")
        self.assertEqual(out["cookies"], [])

    def test_attacker_domain_substring(self):
        # 防御 example.com vs notexample.com 的子串攻击
        from security import sanitize_storage_state
        state = {
            "cookies": [
                {"name": "a", "value": "1", "domain": ".notexample.com"},
            ],
            "origins": [],
        }
        out = sanitize_storage_state(state, "https://example.com/")
        self.assertEqual(out["cookies"], [])


if __name__ == "__main__":
    unittest.main()
