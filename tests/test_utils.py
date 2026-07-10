"""Utility function tests."""
import unittest


class TestUtils(unittest.TestCase):
    """Test utility functions."""

    def test_ip2int_loopback(self):
        """Test IP to int conversion for loopback."""
        from libs.utils import ip2int
        result = ip2int("127.0.0.1")
        self.assertEqual(result, 2130706433)

    def test_int2ip_loopback(self):
        """Test int to IP conversion for loopback."""
        from libs.utils import int2ip
        result = int2ip(2130706433)
        self.assertEqual(result, "127.0.0.1")

    def test_is_lan_private(self):
        """Test LAN detection for private IP."""
        from libs.utils import is_lan
        self.assertTrue(is_lan("192.168.1.1"))
        self.assertTrue(is_lan("10.0.0.1"))
        self.assertTrue(is_lan("172.16.0.1"))

    def test_is_lan_public(self):
        """Test LAN detection for public IP."""
        from libs.utils import is_lan
        self.assertFalse(is_lan("8.8.8.8"))

    def test_is_ip_v4(self):
        """Test IPv4 detection."""
        from libs.utils import is_ip
        self.assertEqual(is_ip("192.168.1.1"), 4)

    def test_is_ip_v6(self):
        """Test IPv6 detection."""
        from libs.utils import is_ip
        self.assertEqual(is_ip("::1"), 6)

    def test_is_ip_invalid(self):
        """Test invalid IP detection."""
        from libs.utils import is_ip
        self.assertEqual(is_ip("not.an.ip"), 0)

    def test_varbinary2ip_v4(self):
        """Test varbinary to IPv4 conversion."""
        from libs.utils import varbinary2ip, ip2varbinary
        ip = "192.168.1.1"
        binary = ip2varbinary(ip, 4)
        result = varbinary2ip(binary)
        self.assertEqual(result, ip)

    def test_get_encodings_from_content_bytes(self):
        """bytes 页面里的 <meta charset> 应被读取到。

        旧实现用 str 正则匹配 bytes 恒抛 TypeError -> 在 find_encoding 里被吞并回退 utf-8,
        GBK 等站点在 charset_normalizer 失手时会被解成乱码, 令中文成功/失败断言失灵。
        """
        from libs._utils.jinja_filters import get_encodings_from_content
        html = '<html><head><meta charset="gb2312"></head></html>'.encode("latin-1")
        encs = [e.lower() for e in get_encodings_from_content(html)]
        self.assertIn("gb2312", encs)

    def test_find_encoding_always_returns_valid_codec(self):
        """find_encoding 绝不能返回非法 codec 名, 否则 decode() 会 LookupError -> 返回 None。

        get_encodings_from_content 的正则可能抓到 '"'(空 charset)或 og:url 里的垃圾串;
        find_encoding 必须用 codecs.lookup 过滤掉, 保证返回值恒可 decode。
        """
        import codecs
        from libs._utils.jinja_filters import find_encoding
        samples = [
            b'<meta charset="">rubbish\xff\xfe',   # 空 charset -> 提取到 '"'
            b'<meta property="og:url" content="http://x?charset=foo">\xff', # 垃圾 charset
            b'<meta charset="gb2312">\xd6\xd0\xce\xc4',
            b'\xff\xfe\x00 binary',
            b'plain ascii',
        ]
        for body in samples:
            enc = find_encoding(body, headers=None)
            codecs.lookup(enc)  # 不得抛 LookupError

    def test_find_encoding_invalid_header_charset_discarded(self):
        """Codex#5: 非法的响应头 charset(如 x-gbk)应被丢弃, 让位给探测/页面内 <meta>,
        而非原样返回导致 decode() -> None。"""
        import codecs
        from libs._utils.jinja_filters import find_encoding
        body = '<meta charset="gb2312">中文签到成功'.encode('gb2312')
        enc = find_encoding(body, headers={"Content-Type": "text/html; charset=invalid-codec-xyz"})
        codecs.lookup(enc)  # 返回值必为合法 codec
        self.assertNotEqual(enc.lower(), "invalid-codec-xyz")


if __name__ == "__main__":
    unittest.main()
