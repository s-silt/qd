#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase-2 smoke tests: FastAPI password-reset endpoints.

Covers:
  1. POST /password/reset  success (user exists) -> generic plain-text message
  2. GET  /password/setnew?token=invalid          -> 400
  3. POST /password/setnew (valid token + new pwd) -> 200 success HTML
"""

import base64
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Conditional skip guard
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

try:
    import umsgpack  # type: ignore
    _UMSGPACK_AVAILABLE = True
except ImportError:
    _UMSGPACK_AVAILABLE = False

_SKIP_MSG = "fastapi / httpx / umsgpack not installed — skipping FastAPI Phase-2 user_passwd tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(db=None):
    from web.fastapi_app import create_app
    return create_app(db=db, fetcher=None, version="test")


class _FakeTemplate:
    """Stub template — embeds its name for easy assertion."""
    def __init__(self, name):
        self._name = name

    def render(self, ns):
        flg = ns.get("flg", "")
        title = ns.get("title", "")
        email_error = ns.get("email_error", "")
        password_error = ns.get("password_error", "")
        return (
            f"<html><!-- template={self._name} flg={flg} title={title} "
            f"email_error={email_error} password_error={password_error} --></html>"
        )


def _patch_jinja(app):
    app.state.jinja_env.get_template = lambda name: _FakeTemplate(name)


def _make_valid_token(db, user: dict) -> str:
    """
    Build a valid base64-encoded reset token that _validate_reset_token()
    will accept.  Uses synchronous doubles to avoid needing a real event loop.
    """
    # The real token is built by: encrypt(userid, [mtime, now]) then encrypt(0, [userid, inner])
    # We'll supply a pre-baked token via the mock decrypt chain instead.
    return "VALID_TOKEN_PLACEHOLDER"


def _make_mock_db(user=None, valid_token_bytes=None):
    """
    Build a minimal mock DB for password-reset tests.

    Parameters
    ----------
    user:
        The dict returned by db.user.get() (None means user not found).
    valid_token_bytes:
        If provided, db.user.decrypt(0, ...) returns (userid, inner_code) and
        db.user.decrypt(userid, inner_code) returns (mtime, recent_time) so
        that _validate_reset_token succeeds.
    """
    db = MagicMock()

    async def _user_get(userid_or_none=None, email=None, fields=None, sql_session=None):
        return user

    db.user.get = AsyncMock(side_effect=_user_get)
    db.user.mod = AsyncMock(return_value=None)
    db.user.encrypt = AsyncMock(side_effect=lambda uid, data: b"encrypted_" + str(uid).encode())

    # Decrypt chain for valid token
    inner_code = b"inner_code_bytes"
    mtime = 12345.0
    now_minus_30s = time.time() - 30  # 30 seconds ago — within 1-hour window

    decrypt_call_count = [0]

    async def _user_decrypt(uid, data, sql_session=None):
        # First call (uid=0) unwraps outer layer → (userid, inner_code)
        # Second call (uid=userid) unwraps inner layer → (mtime, time_time)
        decrypt_call_count[0] += 1
        if uid == 0:
            return (1, inner_code)  # userid=1
        else:
            return (mtime, now_minus_30s)

    db.user.decrypt = AsyncMock(side_effect=_user_decrypt)

    class _FakeTxn:
        async def __aenter__(self):
            return None
        async def __aexit__(self, *args):
            return False

    db.transaction = MagicMock(return_value=_FakeTxn())

    # Stub redis evil counter
    db.redis = MagicMock()
    db.redis.evil = MagicMock()

    return db


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE, _SKIP_MSG)
class TestPasswordResetEndpoints(unittest.TestCase):

    # A user whose mtime matches the token's embedded mtime
    _USER = {
        "id": 1,
        "email": "alice@example.com",
        "mtime": 12345.0,
        "nickname": "alice",
        "role": "user",
        "email_verified": 1,
    }

    # A token whose base64 decodes to something — decrypt mock will handle validation
    _FAKE_VALID_TOKEN = base64.b64encode(b"fake_outer_token").decode()
    _FAKE_INVALID_TOKEN = "!!!not_base64!!!"

    # -----------------------------------------------------------------------
    # Case 1: POST /password/reset  (user exists) → generic 200 plain-text
    # -----------------------------------------------------------------------
    def test_01_password_reset_post_success(self):
        """
        POST /password/reset with a known email returns the generic anti-
        enumeration message and HTTP 200, regardless of whether the user exists.
        """
        import config as _config

        db = _make_mock_db(user=self._USER)
        app = _make_app(db=db)
        _patch_jinja(app)

        # Patch config.domain so the handler doesn't bail out early,
        # and patch _send_reset_mail so no actual mail is sent.
        with patch.object(_config, "domain", "example.com"), \
             patch(
                 "web.fastapi.handlers.user_passwd._send_reset_mail",
                 new=AsyncMock(return_value=None),
             ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/password/reset",
                data={"email": "alice@example.com"},
            )

        self.assertEqual(resp.status_code, 200)
        # The response must be the generic anti-enumeration message
        self.assertIn("如果用户存在", resp.text)

    # -----------------------------------------------------------------------
    # Case 2: GET /password/setnew?token=invalid → 400
    # -----------------------------------------------------------------------
    def test_02_setnew_get_invalid_token_400(self):
        """
        GET /password/setnew with a token that fails validation must return 400.
        """
        # A DB whose decrypt raises an exception (simulates bad token)
        db = _make_mock_db(user=self._USER)

        async def _bad_decrypt(uid, data, sql_session=None):
            raise ValueError("Token mismatch")

        db.user.decrypt = AsyncMock(side_effect=_bad_decrypt)

        app = _make_app(db=db)
        _patch_jinja(app)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/password/setnew?token={self._FAKE_VALID_TOKEN}")
        self.assertEqual(resp.status_code, 400)

    # -----------------------------------------------------------------------
    # Case 3: POST /password/setnew valid token + password → 200 success HTML
    # -----------------------------------------------------------------------
    def test_03_setnew_post_success(self):
        """
        POST /password/setnew with a valid token and a password ≥6 chars
        must update the password and return 200 with success content.
        """
        db = _make_mock_db(user=self._USER)
        app = _make_app(db=db)
        _patch_jinja(app)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/password/setnew",
            data={
                "token": self._FAKE_VALID_TOKEN,
                "password": "newpassword123",
            },
        )

        self.assertEqual(resp.status_code, 200)
        # Should contain a success message with login link
        self.assertIn("密码重置成功", resp.text)
        # db.user.mod should have been called to update the password
        db.user.mod.assert_called_once()
        call_kwargs = db.user.mod.call_args
        self.assertIn("password", call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1])

    # -----------------------------------------------------------------------
    # Case 4: GET /password/reset → 200 with email form template
    # -----------------------------------------------------------------------
    def test_04_password_reset_get_returns_form(self):
        """GET /password/reset renders the email-entry form."""
        db = _make_mock_db()
        app = _make_app(db=db)
        _patch_jinja(app)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/password/reset")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("password_reset_email.html", resp.text)

    # -----------------------------------------------------------------------
    # Case 5: POST /password/setnew short password → 200 with error (not 400)
    # -----------------------------------------------------------------------
    def test_05_setnew_post_short_password_error(self):
        """
        POST /password/setnew with a password shorter than 6 chars should
        return 200 (re-rendered form) with a password_error, not 400.
        """
        db = _make_mock_db(user=self._USER)
        app = _make_app(db=db)
        _patch_jinja(app)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/password/setnew",
            data={
                "token": self._FAKE_VALID_TOKEN,
                "password": "abc",  # too short
            },
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn("password_reset.html", resp.text)

    # -----------------------------------------------------------------------
    # Case 6: All password routes are registered in the app
    # -----------------------------------------------------------------------
    def test_06_routes_registered(self):
        """All password-reset routes should appear in the app route list."""
        db = _make_mock_db()
        app = _make_app(db=db)

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        expected = [
            "/password/reset",
            "/password/setnew",
        ]
        for path in expected:
            self.assertIn(path, paths, f"Route {path!r} not found in app routes")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
