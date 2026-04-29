# -*- coding: utf-8 -*-
"""
Unit tests for the WEB_FRAMEWORK dispatch logic in run.py.

These tests do NOT start an actual server — they use unittest.mock.patch to
replace the launcher functions and only verify that main() calls the correct
one (or raises ValueError for bad values).
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_run():
    """Import (or reload) run.main without actually starting any server.

    We patch the heavy imports before touching run.py so the module can load
    even when optional C-extensions (pbkdf2, pycurl, …) are absent.
    """
    # Provide lightweight stubs for all top-level imports run.py performs.
    stub_modules = {
        "tornado": MagicMock(),
        "tornado.log": MagicMock(),
        "tornado.httpserver": MagicMock(),
        "tornado.ioloop": MagicMock(),
        "config": MagicMock(
            debug=False,
            bind="0.0.0.0",
            port=8923,
            multiprocess=False,
            autoreload=False,
            accesslog=False,
            gzip=False,
            worker_method="Queue",
            check_task_loop=500,
            cookie_secret=b"\x00" * 32,
            aes_key=b"\x00" * 32,
            domain="example.com",
            mail_smtp="",
            mail_password="",
            mailgun_key="",
        ),
        "db": MagicMock(),
        "db.basedb": MagicMock(),
        "db.db_converter": MagicMock(),
        "libs.log": MagicMock(),
        "web.app": MagicMock(),
        "worker": MagicMock(),
    }
    for name, stub in stub_modules.items():
        sys.modules.setdefault(name, stub)

    # Force fresh import of run (in case a previous test already imported it)
    if "run" in sys.modules:
        del sys.modules["run"]

    import run as _run  # noqa: PLC0415
    return _run


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestRunDispatch(unittest.TestCase):
    """Verify that main() dispatches correctly based on WEB_FRAMEWORK."""

    def setUp(self):
        self.run = _import_run()

    # ------------------------------------------------------------------
    # Case 1: WEB_FRAMEWORK=fastapi  -> start_server_fastapi is called
    # ------------------------------------------------------------------
    def test_fastapi_dispatch(self):
        with patch.object(self.run, "start_server_fastapi") as mock_fastapi, \
             patch.object(self.run, "start_server_tornado") as mock_tornado, \
             patch.dict(os.environ, {"WEB_FRAMEWORK": "fastapi"}):
            self.run.main()
        mock_fastapi.assert_called_once()
        mock_tornado.assert_not_called()

    # ------------------------------------------------------------------
    # Case 2: WEB_FRAMEWORK=tornado  -> start_server_tornado is called
    # ------------------------------------------------------------------
    def test_tornado_dispatch(self):
        with patch.object(self.run, "start_server_fastapi") as mock_fastapi, \
             patch.object(self.run, "start_server_tornado") as mock_tornado, \
             patch.dict(os.environ, {"WEB_FRAMEWORK": "tornado"}):
            self.run.main()
        mock_tornado.assert_called_once()
        mock_fastapi.assert_not_called()

    # ------------------------------------------------------------------
    # Case 3: invalid value raises ValueError
    # ------------------------------------------------------------------
    def test_invalid_framework_raises(self):
        with patch.dict(os.environ, {"WEB_FRAMEWORK": "django"}):
            with self.assertRaises(ValueError) as ctx:
                self.run.main()
        self.assertIn("django", str(ctx.exception).lower())

    # ------------------------------------------------------------------
    # Case 4: default (no env var) -> FastAPI is the default
    # ------------------------------------------------------------------
    def test_default_is_fastapi(self):
        env = {k: v for k, v in os.environ.items() if k != "WEB_FRAMEWORK"}
        with patch.object(self.run, "start_server_fastapi") as mock_fastapi, \
             patch.object(self.run, "start_server_tornado") as mock_tornado, \
             patch.dict(os.environ, env, clear=True):
            self.run.main()
        mock_fastapi.assert_called_once()
        mock_tornado.assert_not_called()

    # ------------------------------------------------------------------
    # Case 5: value is case-insensitive ("FastAPI" still routes correctly)
    # ------------------------------------------------------------------
    def test_case_insensitive_fastapi(self):
        with patch.object(self.run, "start_server_fastapi") as mock_fastapi, \
             patch.object(self.run, "start_server_tornado") as mock_tornado, \
             patch.dict(os.environ, {"WEB_FRAMEWORK": "FastAPI"}):
            self.run.main()
        mock_fastapi.assert_called_once()
        mock_tornado.assert_not_called()

    # ------------------------------------------------------------------
    # Case 6: value is case-insensitive ("TORNADO" still routes correctly)
    # ------------------------------------------------------------------
    def test_case_insensitive_tornado(self):
        with patch.object(self.run, "start_server_fastapi") as mock_fastapi, \
             patch.object(self.run, "start_server_tornado") as mock_tornado, \
             patch.dict(os.environ, {"WEB_FRAMEWORK": "TORNADO"}):
            self.run.main()
        mock_tornado.assert_called_once()
        mock_fastapi.assert_not_called()


if __name__ == "__main__":
    unittest.main()
