"""Basic configuration tests."""
import os
import unittest


class TestConfig(unittest.TestCase):
    """Test configuration defaults."""

    def test_config_import(self):
        """Test that config module can be imported."""
        import config
        self.assertIsNotNone(config)

    def test_port_default(self):
        """Test default port value."""
        import config
        self.assertEqual(config.port, int(os.getenv("PORT", "8923")))

    def test_bind_default(self):
        """Test default bind address."""
        import config
        self.assertEqual(config.bind, os.getenv("BIND", "0.0.0.0"))

    def test_db_type_valid(self):
        """Test db_type is a valid option."""
        import config
        self.assertIn(config.db_type, ("sqlite3", "mysql"))


if __name__ == "__main__":
    unittest.main()
