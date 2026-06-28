#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""SSRF 守卫加固单测 (libs.security)。

逐个断言已知 SSRF 绕过向量被拦截:
  - 十进制 / 八进制 / 十六进制 / 短写 IPv4 写法
  - IPv6 映射 / 未指定 / 唯一本地 fc00::/7
  - DNS rebinding (域名解析后再判)
  - 统一 URL 入口
  - storage_state 跨域剔除补充用例
"""

import unittest

from libs import security
from libs.security import (
    resolve_blocked_reason,
    sanitize_storage_state,
)


class TestAltIPNotationBlocked(unittest.TestCase):
    """十进制 / 八进制 / 十六进制 / 短写 IPv4 都应被规范化后拦截。"""

    def test_decimal_loopback(self):
        # 2130706433 == 127.0.0.1
        self.assertTrue(resolve_blocked_reason("2130706433"))

    def test_hex_loopback(self):
        self.assertTrue(resolve_blocked_reason("0x7f000001"))

    def test_octal_loopback(self):
        # 0177.0.0.1 == 127.0.0.1
        self.assertTrue(resolve_blocked_reason("0177.0.0.1"))

    def test_short_form_loopback(self):
        self.assertTrue(resolve_blocked_reason("127.1"))
        self.assertTrue(resolve_blocked_reason("127.0.1"))

    def test_decimal_zero(self):
        # 0 == 0.0.0.0 (unspecified)
        self.assertTrue(resolve_blocked_reason("0"))

    def test_decimal_metadata(self):
        # 2852039166 == 169.254.169.254 (云元数据)
        self.assertTrue(resolve_blocked_reason("2852039166"))

    def test_dotted_hex_metadata(self):
        # 0xa9.0xfe.0xa9.0xfe == 169.254.169.254
        self.assertTrue(resolve_blocked_reason("0xa9.0xfe.0xa9.0xfe"))

    def test_alt_notation_public_allowed(self):
        # 134744072 == 8.8.8.8 (公网) 仍应放行
        self.assertEqual(resolve_blocked_reason("134744072"), "")


class TestIPv6Vectors(unittest.TestCase):
    def test_mapped_loopback_blocked(self):
        self.assertTrue(resolve_blocked_reason("::ffff:127.0.0.1"))

    def test_mapped_metadata_blocked(self):
        self.assertTrue(resolve_blocked_reason("::ffff:169.254.169.254"))

    def test_mapped_hex_loopback_blocked(self):
        # ::ffff:7f00:0001 == ::ffff:127.0.0.1
        self.assertTrue(resolve_blocked_reason("::ffff:7f00:1"))

    def test_unspecified_blocked(self):
        self.assertTrue(resolve_blocked_reason("::"))
        self.assertTrue(resolve_blocked_reason("[::]"))

    def test_loopback6_blocked(self):
        self.assertTrue(resolve_blocked_reason("::1"))

    def test_ula_fc00_default_allowed_opt_in_blocked(self):
        # 唯一本地 fc00::/7 与 RFC1918 私网策略一致: 默认放行, 收紧时拦截
        self.assertEqual(resolve_blocked_reason("fc00::1"), "")
        old = security.BLOCK_PRIVATE_IP
        try:
            security.BLOCK_PRIVATE_IP = True
            self.assertTrue(resolve_blocked_reason("fc00::1"))
            self.assertTrue(resolve_blocked_reason("fd12:3456::1"))
        finally:
            security.BLOCK_PRIVATE_IP = old

    def test_mapped_public_allowed(self):
        self.assertEqual(resolve_blocked_reason("::ffff:8.8.8.8"), "")


class TestFullSegments(unittest.TestCase):
    def test_loopback_full_8(self):
        for h in ("127.0.0.1", "127.0.0.2", "127.1.2.3", "127.255.255.254"):
            self.assertTrue(resolve_blocked_reason(h), f"{h} 应被拦截")

    def test_link_local_full_16(self):
        for h in ("169.254.0.1", "169.254.169.254", "169.254.255.255"):
            self.assertTrue(resolve_blocked_reason(h), f"{h} 应被拦截")


class TestDNSRebinding(unittest.TestCase):
    """域名必须解析后再判, 解析到内网/元数据则拦截。"""

    def _patch_getaddrinfo(self, mapping):
        def fake(host, *a, **k):
            ip = mapping[host]
            return [(2, 1, 6, "", (ip, 0))]
        return fake

    def test_domain_resolving_to_metadata_blocked(self):
        orig = security.socket.getaddrinfo
        security.socket.getaddrinfo = self._patch_getaddrinfo(
            {"rebind.evil.test": "169.254.169.254"}
        )
        try:
            self.assertTrue(resolve_blocked_reason("rebind.evil.test"))
        finally:
            security.socket.getaddrinfo = orig

    def test_domain_resolving_to_loopback_blocked(self):
        orig = security.socket.getaddrinfo
        security.socket.getaddrinfo = self._patch_getaddrinfo(
            {"rebind2.evil.test": "127.0.0.1"}
        )
        try:
            self.assertTrue(resolve_blocked_reason("rebind2.evil.test"))
        finally:
            security.socket.getaddrinfo = orig

    def test_domain_resolving_to_public_allowed(self):
        orig = security.socket.getaddrinfo
        security.socket.getaddrinfo = self._patch_getaddrinfo(
            {"good.test": "93.184.216.34"}
        )
        try:
            self.assertEqual(resolve_blocked_reason("good.test"), "")
        finally:
            security.socket.getaddrinfo = orig


class TestURLEntryPoint(unittest.TestCase):
    """给定完整 URL 的统一入口 (供 fetcher/playwright/ocr 复用)。"""

    def test_url_entry_blocks_internal(self):
        from libs.security import resolve_url_blocked_reason
        self.assertTrue(
            resolve_url_blocked_reason("http://169.254.169.254/latest/meta-data/")
        )
        self.assertTrue(
            resolve_url_blocked_reason("http://2130706433:8080/admin")
        )

    def test_url_entry_allows_public(self):
        from libs.security import resolve_url_blocked_reason
        self.assertEqual(
            resolve_url_blocked_reason("https://example.com/path"), ""
        )

    def test_url_entry_missing_host(self):
        from libs.security import resolve_url_blocked_reason
        self.assertTrue(resolve_url_blocked_reason("not-a-url"))


class TestStorageStateExtra(unittest.TestCase):
    def test_substring_attack_dropped(self):
        # notexample.com 不应被当作 example.com 的子域
        state = {
            "cookies": [
                {"name": "ok", "value": "1", "domain": "example.com"},
                {"name": "evil", "value": "x", "domain": "notexample.com"},
            ],
            "origins": [],
        }
        out = sanitize_storage_state(state, "https://example.com/")
        names = [c["name"] for c in out["cookies"]]
        self.assertIn("ok", names)
        self.assertNotIn("evil", names)

    def test_cross_origin_origins_dropped(self):
        state = {
            "cookies": [],
            "origins": [
                {"origin": "https://example.com", "localStorage": []},
                {"origin": "https://attacker.com", "localStorage": []},
            ],
        }
        out = sanitize_storage_state(state, "https://example.com/")
        origins = [o["origin"] for o in out["origins"]]
        self.assertIn("https://example.com", origins)
        self.assertNotIn("https://attacker.com", origins)

    def test_file_url_drops_everything(self):
        state = {
            "cookies": [{"name": "a", "value": "1", "domain": "example.com"}],
            "origins": [{"origin": "https://example.com"}],
        }
        out = sanitize_storage_state(state, "file:///etc/passwd")
        self.assertEqual(out["cookies"], [])
        self.assertEqual(out["origins"], [])

    def test_subdomain_cookie_kept(self):
        state = {
            "cookies": [{"name": "a", "value": "1", "domain": ".example.com"}],
            "origins": [],
        }
        out = sanitize_storage_state(state, "https://sub.example.com/")
        self.assertEqual(len(out["cookies"]), 1)


if __name__ == "__main__":
    unittest.main()
