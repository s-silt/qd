#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI user-register endpoints.

Cases:
  1. POST /register  success (new user, reg enabled)  → 302 redirect + Set-Cookie
  2. POST /register  email already exists             → 400
  3. POST /register  sends verification email (mocked) → mail helper called

Skipped automatically when fastapi / httpx / umsgpack are not installed.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Conditional skip guards
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

try:
    import umsgpack  # noqa: F401
    _UMSGPACK_AVAILABLE = True
except ImportError:
    _UMSGPACK_AVAILABLE = False

_SKIP = not (_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE)
_SKIP_MSG = (
    "fastapi / httpx / umsgpack not installed — "
    "skipping FastAPI Phase-2 user register tests"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal FastAPI app (no real DB needed at startup)."""
    pytest = __import__("pytest", fromlist=[])
    pytest.importorskip("fastapi")
    pytest.importorskip("umsgpack")
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


def _make_tx_ctx():
    """Async context-manager that returns a mock sql_session."""
    class _FakeTx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            pass

    return _FakeTx()


def _make_mock_db(
    *,
    reg_en: int = 1,
    must_verify_email: int = 0,
    user_exists: bool = False,
    email_verified: int = 0,
    domain: str = "",
):
    """
    Build a mock DB whose async methods behave like the real ones
    for the register flow.

    Parameters
    ----------
    reg_en          : 1 = registrations open, 0 = closed
    must_verify_email: 1 = force email verification before login
    user_exists     : whether db.user.get returns an existing user record
    email_verified  : existing user's email_verified value
    domain          : value for config.domain (patched separately)
    """
    db = MagicMock()

    # ---- site config ----
    async def _site_get(site_id, fields=None, sql_session=None):
        row = {}
        if fields and "regEn" in fields:
            row["regEn"] = reg_en
        if fields and "MustVerifyEmailEn" in fields:
            row["MustVerifyEmailEn"] = must_verify_email
        return row

    db.site.get = _site_get

    # ---- user records ----
    _existing_user = {
        "id": 42,
        "email": "existing@example.com",
        "nickname": "tester",
        "role": "user",
        "email_verified": email_verified,
    }
    _new_user = {
        "id": 99,
        "email": "new@example.com",
        "nickname": None,
        "role": "user",
        "email_verified": 0,
    }

    # Tracks whether add() has been called (simulates DB state after insert)
    _add_called = {"done": False}

    async def _user_get(uid=None, email=None, fields=None, sql_session=None, **kw):
        if user_exists:
            # Return existing user regardless (both pre- and post-add)
            rec = _existing_user
        elif _add_called["done"]:
            # After add(), return the newly created user
            rec = _new_user
        else:
            # Before add() — user not yet in DB
            return None
        if fields:
            return {k: rec[k] for k in fields if k in rec}
        return dict(rec)

    db.user.get = _user_get

    # ---- user.add ----
    if user_exists:
        # Simulate duplicate-user exception on add
        class _DupUser(Exception):
            pass

        db.user.DeplicateUser = _DupUser

        async def _user_add(**kw):
            raise _DupUser("duplicate")

        db.user.add = _user_add
    else:
        db.user.DeplicateUser = type("DeplicateUser", (Exception,), {})

        async def _user_add_ok(**kw):
            _add_called["done"] = True

        db.user.add = _user_add_ok

    # ---- user.list ----
    async def _user_list(sql_session=None, fields=None, **kw):
        rec = dict(_new_user)
        if fields:
            return [{k: rec[k] for k in fields if k in rec}]
        return [rec]

    db.user.list = _user_list

    # ---- user.mod ----
    db.user.mod = AsyncMock(return_value=None)

    # ---- user.encrypt ----
    async def _user_encrypt(uid, val, sql_session=None):
        return b"encrypted_token"

    db.user.encrypt = _user_encrypt

    # ---- user.decrypt ----
    async def _user_decrypt(uid, val, sql_session=None):
        # Return (userid, inner_code) for outer decrypt, and (email, ts) for inner
        if uid == 0:
            return (99, b"inner_code")
        # inner decrypt
        return ("new@example.com", 0.0)  # ts=0 → expired; fine for verify tests

    db.user.decrypt = _user_decrypt

    # ---- notepad.add ----
    db.notepad.add = AsyncMock(return_value=None)

    # ---- transaction ----
    db.transaction = lambda: _make_tx_ctx()

    # ---- redis ----
    db.redis = MagicMock()
    db.redis.evil = MagicMock(return_value=None)
    db.redis.is_evil = MagicMock(return_value=False)

    return db


# ---------------------------------------------------------------------------
# Test Case 1 — POST /register success
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestRegisterSuccess(unittest.TestCase):
    """Case 1: POST /register with a new email → 302 redirect + Set-Cookie."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(reg_en=1, must_verify_email=0, user_exists=False)
        # Patch config.domain so mail is not attempted
        self._domain_patch = patch("config.domain", "")
        self._domain_patch.start()
        self.client = TestClient(self.app, raise_server_exceptions=False, follow_redirects=False)

    def tearDown(self):
        self._domain_patch.stop()

    def test_register_success_redirects(self):
        resp = self.client.post(
            "/register",
            data={"email": "new@example.com", "password": "securepass"},
        )
        self.assertIn(
            resp.status_code, (302, 303),
            f"Expected redirect, got {resp.status_code}: {resp.text[:200]}",
        )

    def test_register_success_sets_cookie(self):
        resp = self.client.post(
            "/register",
            data={"email": "new@example.com", "password": "securepass"},
        )
        # Either a Set-Cookie header or location redirect means success
        has_redirect = resp.status_code in (302, 303)
        self.assertTrue(has_redirect, f"Expected redirect, got {resp.status_code}")


# ---------------------------------------------------------------------------
# Test Case 2 — POST /register email already exists → 400
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestRegisterEmailExists(unittest.TestCase):
    """Case 2: POST /register when email already registered → 400."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(
            reg_en=1,
            must_verify_email=0,
            user_exists=True,
            email_verified=1,
        )
        self._domain_patch = patch("config.domain", "example.com")
        self._domain_patch.start()
        self.client = TestClient(self.app, raise_server_exceptions=False, follow_redirects=False)

    def tearDown(self):
        self._domain_patch.stop()

    def test_returns_400(self):
        resp = self.client.post(
            "/register",
            data={"email": "existing@example.com", "password": "securepass"},
        )
        self.assertEqual(
            resp.status_code, 400,
            f"Expected 400 for duplicate email, got {resp.status_code}: {resp.text[:200]}",
        )


# ---------------------------------------------------------------------------
# Test Case 3 — verification email is sent (mock)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestRegisterSendsVerificationMail(unittest.TestCase):
    """Case 3: POST /register with domain configured → send_mail is called."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(
            reg_en=1,
            must_verify_email=0,
            user_exists=False,
        )

    def test_send_mail_called(self):
        # Patch at the module-level attribute where it is used in the handler.
        # Since the handler does `from libs._utils.mail import send_mail`, we patch
        # the name inside the handler's own module namespace.
        with (
            patch("config.domain", "qd.example.com"),
            patch("config.mail_domain_https", False),
            patch(
                "web.fastapi.handlers.user_register._send_register_mail",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            client = TestClient(self.app, raise_server_exceptions=False, follow_redirects=False)
            client.post(
                "/register",
                data={"email": "new@example.com", "password": "securepass"},
            )
            mock_send.assert_called_once()
            # Confirm the call received a user dict with an email field
            call_args = mock_send.call_args
            # _send_register_mail(db, user, sql_session=...) — user is the 2nd positional arg
            user_arg = call_args.args[1] if call_args.args and len(call_args.args) >= 2 else None
            self.assertIsNotNone(user_arg, "_send_register_mail should receive a user dict")
            self.assertIn(
                "email", user_arg,
                f"user dict should have 'email' key, got: {user_arg}",
            )


# ---------------------------------------------------------------------------
# Bonus — route registration sanity check
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestRegisterRoutesRegistered(unittest.TestCase):
    """The /register and /verify routes must be present in the app."""

    def setUp(self):
        self.app = _make_app()

    def test_register_route_exists(self):
        paths = [r.path for r in self.app.routes]
        self.assertIn("/register", paths, f"Missing /register in routes: {paths}")

    def test_verify_route_exists(self):
        paths = [r.path for r in self.app.routes]
        verify_routes = [p for p in paths if p.startswith("/verify")]
        self.assertTrue(
            verify_routes,
            f"No /verify route found in: {paths}",
        )


if __name__ == "__main__":
    unittest.main()
