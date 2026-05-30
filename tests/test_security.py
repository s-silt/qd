#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""libs.security 的纯函数单测 (SSRF 防护 + storage_state 跨域剔除)。"""

import unittest

from libs import security
from libs.security import (
    domain_matches,
    parse_cookie_str_to_storage_state,
    resolve_blocked_reason,
    sanitize_storage_state,
)


class TestSSRFGuard(unittest.TestCase):
    def test_metadata_and_loopback_blocked(self):
        for host in ("169.254.169.254", "127.0.0.1", "::1", "localhost", "224.0.0.1"):
            self.assertTrue(resolve_blocked_reason(host), f"{host} 应被拦截")

    def test_public_and_private_allowed_by_default(self):
        # 默认放行公网与内网 (内网部署场景)
        for host in ("8.8.8.8", "example.com", "10.0.0.5", "192.168.1.1"):
            self.assertEqual(resolve_blocked_reason(host), "", f"{host} 不应被拦截")

    def test_block_private_opt_in(self):
        old = security.BLOCK_PRIVATE_IP
        try:
            security.BLOCK_PRIVATE_IP = True
            self.assertTrue(resolve_blocked_reason("10.0.0.5"))
            self.assertTrue(resolve_blocked_reason("192.168.1.1"))
            self.assertEqual(resolve_blocked_reason("8.8.8.8"), "")
        finally:
            security.BLOCK_PRIVATE_IP = old

    def test_empty_host(self):
        self.assertTrue(resolve_blocked_reason(""))


class TestStorageStateSanitize(unittest.TestCase):
    def test_domain_matches(self):
        self.assertTrue(domain_matches(".example.com", "a.example.com"))
        self.assertTrue(domain_matches("example.com", "example.com"))
        self.assertFalse(domain_matches("evil.com", "example.com"))

    def test_cross_domain_cookies_dropped(self):
        state = {
            "cookies": [
                {"name": "a", "value": "1", "domain": ".example.com"},
                {"name": "evil", "value": "x", "domain": "attacker.com"},
            ],
            "origins": [],
        }
        out = sanitize_storage_state(state, "https://example.com/checkin")
        names = [c["name"] for c in out["cookies"]]
        self.assertIn("a", names)
        self.assertNotIn("evil", names)

    def test_parse_cookie_str(self):
        out = parse_cookie_str_to_storage_state("k1=v1; k2=v2", "https://example.com")
        names = {c["name"]: c["value"] for c in out["cookies"]}
        self.assertEqual(names, {"k1": "v1", "k2": "v2"})


if __name__ == "__main__":
    unittest.main()
