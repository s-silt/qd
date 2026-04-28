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


if __name__ == "__main__":
    unittest.main()
