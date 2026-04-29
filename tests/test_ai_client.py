"""Tests for libs.ai_client (HAR preprocessing & response parsing)."""
import asyncio
import unittest


class TestPreprocessHar(unittest.TestCase):
    def _make_entry(self, url, method="GET", mime="application/json", body=""):
        return {
            "request": {
                "method": method,
                "url": url,
                "headers": [
                    {"name": "Content-Type", "value": "application/json"},
                    {"name": "User-Agent", "value": "x"},  # 应被过滤
                ],
                "cookies": [{"name": "session", "value": "abc"}],
                "postData": {"mimeType": "application/json", "text": body},
            },
            "response": {
                "status": 200,
                "content": {"mimeType": mime, "text": "ok"},
            },
        }

    def test_filters_static_resources(self):
        from libs.ai_client import preprocess_har
        har = {
            "log": {
                "entries": [
                    self._make_entry("https://x.com/a.js", mime="application/javascript"),
                    self._make_entry("https://x.com/style.css", mime="text/css"),
                    self._make_entry("https://x.com/img.png", mime="image/png"),
                    self._make_entry("https://x.com/api/sign", method="POST"),
                ]
            }
        }
        out = preprocess_har(har, max_entries=10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://x.com/api/sign")
        self.assertEqual(out[0]["method"], "POST")

    def test_filters_analytics(self):
        from libs.ai_client import preprocess_har
        har = {
            "log": {
                "entries": [
                    self._make_entry("https://www.google-analytics.com/collect"),
                    self._make_entry("https://api.x.com/check_in", method="POST"),
                ]
            }
        }
        out = preprocess_har(har, max_entries=10)
        urls = [e["url"] for e in out]
        self.assertNotIn("https://www.google-analytics.com/collect", urls)
        self.assertIn("https://api.x.com/check_in", urls)

    def test_keeps_relevant_headers_only(self):
        from libs.ai_client import preprocess_har
        har = {"log": {"entries": [self._make_entry("https://x.com/api/sign", "POST")]}}
        out = preprocess_har(har, max_entries=10)
        names = {h["name"].lower() for h in out[0]["headers"]}
        self.assertIn("content-type", names)
        self.assertNotIn("user-agent", names)

    def test_max_entries_limit(self):
        from libs.ai_client import preprocess_har
        har = {
            "log": {
                "entries": [
                    self._make_entry(f"https://x.com/api/n{i}", "POST")
                    for i in range(50)
                ]
            }
        }
        out = preprocess_har(har, max_entries=5)
        self.assertEqual(len(out), 5)

    def test_post_methods_prioritized(self):
        from libs.ai_client import preprocess_har
        har = {
            "log": {
                "entries": [
                    self._make_entry("https://x.com/list", "GET"),
                    self._make_entry("https://x.com/sign", "POST"),
                ]
            }
        }
        out = preprocess_har(har, max_entries=10)
        # POST 应排在前面
        self.assertEqual(out[0]["method"], "POST")


class TestParseAIResponse(unittest.TestCase):
    def test_plain_json(self):
        from libs.ai_client import parse_ai_response
        result = parse_ai_response('{"sitename": "x", "entries": []}')
        self.assertEqual(result["sitename"], "x")

    def test_markdown_wrapped(self):
        from libs.ai_client import parse_ai_response
        result = parse_ai_response('```json\n{"sitename": "y", "entries": []}\n```')
        self.assertEqual(result["sitename"], "y")

    def test_with_extra_text(self):
        from libs.ai_client import parse_ai_response
        result = parse_ai_response(
            'Here is the result:\n{"sitename": "z", "entries": []}\nThanks.'
        )
        self.assertEqual(result["sitename"], "z")

    def test_invalid_json_raises(self):
        from libs.ai_client import AIClientError, parse_ai_response
        with self.assertRaises(AIClientError):
            parse_ai_response("not json at all")

    def test_empty_raises(self):
        from libs.ai_client import AIClientError, parse_ai_response
        with self.assertRaises(AIClientError):
            parse_ai_response("")
        with self.assertRaises(AIClientError):
            parse_ai_response("   \n  ")

    def test_uppercase_fence(self):
        from libs.ai_client import parse_ai_response
        # ```JSON 大写也接受
        result = parse_ai_response('```JSON\n{"sitename": "u", "entries": []}\n```')
        self.assertEqual(result["sitename"], "u")

    def test_indented_inside_fence(self):
        from libs.ai_client import parse_ai_response
        # 围栏内部前后有空白也能解析
        result = parse_ai_response(
            'Sure!\n```json\n  \n{"sitename": "i", "entries": []}\n  \n```\n'
        )
        self.assertEqual(result["sitename"], "i")

    def test_nested_braces(self):
        from libs.ai_client import parse_ai_response
        result = parse_ai_response(
            '解析结果:\n{"sitename":"n","entries":[{"url":"x","headers":[]}]}\n done'
        )
        self.assertEqual(len(result["entries"]), 1)


class TestPreprocessTruncate(unittest.TestCase):
    """验证 body / header 截断长度可由配置覆盖。"""

    def _entry(self, body, header_value):
        return {
            "request": {
                "method": "POST",
                "url": "https://x.com/api/sign",
                "headers": [{"name": "Content-Type", "value": header_value}],
                "cookies": [],
                "postData": {"mimeType": "application/json", "text": body},
            },
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": "ok"}},
        }

    def test_explicit_truncate(self):
        from libs.ai_client import preprocess_har
        big_body = "x" * 2000
        big_header = "y" * 1000
        har = {"log": {"entries": [self._entry(big_body, big_header)]}}
        out = preprocess_har(har, max_entries=5, body_truncate=100, header_truncate=50)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["body"].startswith("x" * 100))
        self.assertIn("(truncated)", out[0]["body"])
        self.assertEqual(len(out[0]["headers"][0]["value"]), 50)

    def test_default_truncate_from_config(self):
        from libs.ai_client import preprocess_har
        # 不传 truncate 参数, 走 config 默认值 (500/200)
        har = {"log": {"entries": [self._entry("a" * 1000, "b" * 500)]}}
        out = preprocess_har(har, max_entries=5)
        self.assertTrue(out[0]["body"].startswith("a" * 500))
        self.assertEqual(len(out[0]["headers"][0]["value"]), 200)


class TestAIResultToHar(unittest.TestCase):
    def test_basic_conversion(self):
        from libs.ai_client import ai_result_to_har
        result = {
            "sitename": "Demo",
            "entries": [
                {
                    "method": "POST",
                    "url": "https://api.demo.com/sign",
                    "headers": [
                        {"name": "Content-Type", "value": "application/json"}
                    ],
                    "body": '{"action":"sign"}',
                    "reason": "POST 路径含 sign",
                }
            ],
        }
        har = ai_result_to_har(result)
        self.assertEqual(len(har["log"]["entries"]), 1)
        e = har["log"]["entries"][0]
        self.assertEqual(e["request"]["method"], "POST")
        self.assertEqual(e["request"]["url"], "https://api.demo.com/sign")
        self.assertIn("postData", e["request"])
        self.assertEqual(e["request"]["postData"]["text"], '{"action":"sign"}')

    def test_empty_entries(self):
        from libs.ai_client import ai_result_to_har
        har = ai_result_to_har({"entries": []})
        self.assertEqual(har["log"]["entries"], [])


class TestAIClientDisabled(unittest.TestCase):
    def test_disabled_when_no_key(self):
        from libs.ai_client import AIClient
        client = AIClient(api_key="")
        self.assertFalse(client.enabled)


async def _gen(chunks):
    """把 list[bytes] 包成 async iterator, 模拟 aiohttp 流。"""
    for c in chunks:
        yield c


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestReadCapped(unittest.TestCase):
    """流式读取 + 大小上限 (用于 HARAutoCapture sidecar 响应)。"""

    def test_under_limit(self):
        from libs.ai_client import read_capped
        body = _run(read_capped(_gen([b"abc", b"de", b""]), max_bytes=100))
        self.assertEqual(body, b"abcde")

    def test_at_limit(self):
        from libs.ai_client import read_capped
        body = _run(read_capped(_gen([b"a" * 50, b"b" * 50]), max_bytes=100))
        self.assertEqual(len(body), 100)

    def test_exceeds_limit_raises(self):
        from libs.ai_client import HARSizeLimitExceeded, read_capped
        with self.assertRaises(HARSizeLimitExceeded) as ctx:
            _run(read_capped(_gen([b"x" * 50, b"x" * 60]), max_bytes=100))
        self.assertEqual(ctx.exception.limit, 100)
        self.assertGreater(ctx.exception.received, 100)

    def test_first_chunk_already_too_big(self):
        from libs.ai_client import HARSizeLimitExceeded, read_capped
        with self.assertRaises(HARSizeLimitExceeded):
            _run(read_capped(_gen([b"x" * 200]), max_bytes=100))

    def test_empty_stream(self):
        from libs.ai_client import read_capped
        self.assertEqual(_run(read_capped(_gen([]), max_bytes=100)), b"")


if __name__ == "__main__":
    unittest.main()
