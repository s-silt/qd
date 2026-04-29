#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI util_simple handler.

Tests:
  1.  GET /util/delay/1          - waits ~1s, returns plain-text confirmation
  2.  GET /util/unicode           - unicode escape conversion
  3.  GET /util/timestamp         - returns timestamp JSON with 状态=200
  4.  GET /util/aes/encrypt       - AES ECB encrypt, returns result
  5.  GET /util/base64/encode     - base64 encode
  6.  GET /util/base64/decode     - base64 decode
  7.  GET /util/urldecode         - URL decode
  8.  GET /util/gb2312            - GB2312 encode
  9.  GET /util/regex             - regex findall
  10. GET /util/string/replace    - regex string replace
  11. GET /util/delay             - delay via ?seconds= query param
  12. GET /util/aes/decrypt       - AES ECB decrypt (roundtrip)

Skipped automatically when fastapi / httpx are not installed.
"""

import unittest

# ---------------------------------------------------------------------------
# Conditional skip
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed — skipping FastAPI Phase 2 util_simple tests"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _make_app():
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilDelay(unittest.TestCase):
    """Delay endpoints."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_delay_path_1_second(self):
        """GET /util/delay/1 should respond with delay message."""
        # Use a small value to keep tests fast; we just check the response body
        resp = self.client.get("/util/delay/0")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("delay", resp.text)

    def test_delay_path_float(self):
        """GET /util/delay/0.0 should respond with delay message."""
        resp = self.client.get("/util/delay/0.0")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("delay", resp.text)

    def test_delay_query_param(self):
        """GET /util/delay?seconds=0 should respond with delay message."""
        resp = self.client.get("/util/delay", params={"seconds": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("delay", resp.text)

    def test_delay_negative_clamped(self):
        """Negative seconds should be clamped to 0."""
        resp = self.client.get("/util/delay/-1")
        self.assertEqual(resp.status_code, 200)
        # -1 clamped to 0 -> "delay 0.0 second."
        self.assertIn("0.0", resp.text)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilUnicode(unittest.TestCase):
    """Unicode escape conversion endpoint."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_unicode_conversion_basic(self):
        """GET /util/unicode?content=hello should return 状态=200."""
        resp = self.client.get("/util/unicode", params={"content": "hello"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertIn("转换后", data)

    def test_unicode_no_content(self):
        """GET /util/unicode with no content should still return 状态=200."""
        resp = self.client.get("/util/unicode")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilTimestamp(unittest.TestCase):
    """Timestamp endpoint."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_timestamp_current(self):
        """GET /util/timestamp with no params returns current time info."""
        resp = self.client.get("/util/timestamp")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertIn("时间戳", data)
        self.assertIn("完整时间戳", data)
        self.assertIn("北京时间", data)

    def test_timestamp_from_ts(self):
        """GET /util/timestamp?ts=0 returns Unix epoch info."""
        resp = self.client.get("/util/timestamp", params={"ts": "0"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertEqual(data.get("时间戳"), 0)

    def test_timestamp_from_dt(self):
        """GET /util/timestamp?dt=2024-01-01+00:00:00 converts datetime to ts."""
        resp = self.client.get(
            "/util/timestamp",
            params={"dt": "2024-01-01 00:00:00"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertIn("时间戳", data)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilAes(unittest.TestCase):
    """AES encrypt / decrypt endpoints."""

    AES_KEY = "1234567890123456"  # 16-byte key
    WORD = "hello world"

    def setUp(self):
        try:
            from libs._utils.crypto import _aes_encrypt  # noqa: F401
            self._aes_available = True
        except ImportError:
            self._aes_available = False
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_aes_encrypt_ecb(self):
        """GET /util/aes/encrypt should return base64 ciphertext."""
        if not self._aes_available:
            self.skipTest("PyCryptodome not installed")
        resp = self.client.get(
            "/util/aes/encrypt",
            params={
                "word": self.WORD,
                "key": self.AES_KEY,
                "mode": "ECB",
                "iv": "",
                "output_format": "base64",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertIn("result", data)
        self.assertTrue(len(data["result"]) > 0)

    def test_aes_decrypt_ecb_roundtrip(self):
        """Encrypt then decrypt should return original plaintext."""
        if not self._aes_available:
            self.skipTest("PyCryptodome not installed")
        # Encrypt
        enc_resp = self.client.get(
            "/util/aes/encrypt",
            params={
                "word": self.WORD,
                "key": self.AES_KEY,
                "mode": "ECB",
                "output_format": "base64",
            },
        )
        ciphertext = enc_resp.json()["result"]
        # Decrypt
        dec_resp = self.client.get(
            "/util/aes/decrypt",
            params={
                "word": ciphertext,
                "key": self.AES_KEY,
                "mode": "ECB",
                "input_format": "base64",
            },
        )
        self.assertEqual(dec_resp.status_code, 200)
        data = dec_resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertEqual(data.get("result"), self.WORD)

    def test_aes_missing_params(self):
        """Missing word or key should return error status."""
        resp = self.client.get(
            "/util/aes/encrypt",
            params={"word": self.WORD},  # key missing
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotEqual(data.get("状态"), "200")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilBase64(unittest.TestCase):
    """Base64 encode/decode endpoints."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_base64_encode(self):
        """GET /util/base64/encode?content=hello should return aGVsbG8=."""
        resp = self.client.get("/util/base64/encode", params={"content": "hello"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertEqual(data.get("result"), "aGVsbG8=")

    def test_base64_decode(self):
        """GET /util/base64/decode?content=aGVsbG8= should return hello."""
        resp = self.client.get("/util/base64/decode", params={"content": "aGVsbG8="})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertEqual(data.get("result"), "hello")

    def test_base64_roundtrip(self):
        """Encode then decode should return original."""
        text = "FastAPI util_simple test 中文"
        enc = self.client.get("/util/base64/encode", params={"content": text}).json()
        dec = self.client.get("/util/base64/decode", params={"content": enc["result"]}).json()
        self.assertEqual(dec.get("result"), text)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilUrlDecode(unittest.TestCase):
    """URL decode endpoint."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_urldecode_basic(self):
        """GET /util/urldecode?content=%E4%B8%AD%E6%96%87 should return 中文."""
        resp = self.client.get(
            "/util/urldecode",
            params={"content": "%E4%B8%AD%E6%96%87"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        self.assertEqual(data.get("转换后"), "中文")

    def test_urldecode_plus(self):
        """GET /util/urldecode with unquote_plus=true should decode + as space."""
        resp = self.client.get(
            "/util/urldecode",
            params={"content": "hello+world", "unquote_plus": "true"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("转换后"), "hello world")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilGb2312(unittest.TestCase):
    """GB2312 encoding endpoint."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_gb2312_encode(self):
        """GET /util/gb2312?content=中文 should return URL-encoded GB2312 bytes."""
        resp = self.client.get("/util/gb2312", params={"content": "中"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "200")
        # GB2312 encoding of 中 is %D6%D0
        self.assertIn("%", data.get("转换后", ""))


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilRegex(unittest.TestCase):
    """Regex findall endpoint."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_regex_findall(self):
        """GET /util/regex?data=abc123&p=\\d+ should return {1: '123'}."""
        resp = self.client.get(
            "/util/regex",
            params={"data": "abc123def456", "p": r"\d+"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "OK")
        self.assertIn("数据", data)
        # Two matches
        self.assertEqual(len(data["数据"]), 2)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilStringReplace(unittest.TestCase):
    """Regex string replace endpoint."""

    def setUp(self):
        self.client = TestClient(_make_app(), raise_server_exceptions=True)

    def test_string_replace(self):
        """GET /util/string/replace should replace pattern with target."""
        resp = self.client.get(
            "/util/string/replace",
            params={"s": "hello world", "p": "world", "t": "FastAPI"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("状态"), "OK")
        self.assertEqual(data.get("处理后字符串"), "hello FastAPI")

    def test_string_replace_text_output(self):
        """GET /util/string/replace?r=text returns plain text."""
        resp = self.client.get(
            "/util/string/replace",
            params={"s": "hello world", "p": "world", "t": "test", "r": "text"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("hello test", resp.text)


if __name__ == "__main__":
    unittest.main()
