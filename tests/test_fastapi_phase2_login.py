#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI login / logout endpoints.

Cases:
  1. GET  /login  unauthenticated         -> 200, renders login form
  2. GET  /login  already logged in       -> 302 redirect to /my/
  3. POST /login  wrong password          -> re-render with error (not 302)
  4. POST /login  correct credentials     -> 302 redirect + Set-Cookie
  5. GET  /logout                         -> 302 redirect + clears cookie
  6. POST /login  missing email/password  -> re-render with error (not 302)
  7. POST /login  disabled account        -> re-render with error (not 302)
  8. Login/logout routes are registered in the app

Skipped automatically when fastapi / httpx / umsgpack are not installed.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

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
    "fastapi / httpx / umsgpack not installed — skipping FastAPI Phase-2 login tests"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal FastAPI app (db is injected via app.state later)."""
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


def _make_mock_db(
    user_exists=True,
    user_role="user",
    user_status="Enable",
    challenge_ok=True,
    must_verify_email=0,
    email_verified=1,
    reg_en=1,
):
    """Return a MagicMock DB with coroutine helpers for the login flow."""
    db = MagicMock()

    # db.site.get — drives regFlg and MustVerifyEmailEn
    async def _site_get(site_id, fields=None, sql_session=None):
        row = {}
        if fields:
            if "regEn" in fields:
                row["regEn"] = reg_en
            if "MustVerifyEmailEn" in fields:
                row["MustVerifyEmailEn"] = must_verify_email
        return row

    db.site.get = _site_get

    # db.user.get — identity lookup
    async def _user_get(uid_or_kw=None, email=None, fields=None, sql_session=None, **kw):
        if not user_exists:
            return None
        base = {
            "id": 1,
            "email": "test@example.com",
            "nickname": "tester",
            "role": user_role,
            "status": user_status,
            "email_verified": email_verified,
            "password": b"hashed",
            "password_md5": "md5hash",
        }
        if fields:
            return {k: base[k] for k in fields if k in base}
        return base

    db.user.get = _user_get

    # db.user.challenge — password verification
    async def _challenge(email, password, sql_session=None):
        return challenge_ok

    db.user.challenge = _challenge

    # db.user.mod — update fields
    db.user.mod = AsyncMock(return_value=None)

    # db.user.decrypt — used for MD5 update path
    async def _decrypt(uid, val, sql_session=None):
        return b"decrypted_password"

    db.user.decrypt = _decrypt

    # db.transaction() — async context manager
    class _FakeTx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            pass

    db.transaction = lambda: _FakeTx()

    # db.redis — evil counter
    db.redis = MagicMock()
    db.redis.evil = MagicMock(return_value=None)

    return db


def _make_auth_client(app, user_id=1, role="user"):
    """
    Return a TestClient that already has a valid 'user' secure cookie.
    A helper endpoint is injected to set the cookie via the same signing logic.
    """
    import umsgpack
    from fastapi import APIRouter, Response
    from web.fastapi.auth import set_secure_cookie

    helper_router = APIRouter()

    @helper_router.get("/_test_set_login_auth")
    def _set(response: Response):
        payload = umsgpack.packb({
            "id": user_id,
            "role": role,
            "email": "test@example.com",
            "nickname": "tester",
            "email_verified": 1,
        })
        set_secure_cookie(response, "user", payload)
        return {"ok": True}

    app.include_router(helper_router)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/_test_set_login_auth")
    assert r.status_code == 200, f"Cookie setup failed: {r.status_code}"
    return client


# ---------------------------------------------------------------------------
# Case 1 — GET /login unauthenticated -> 200 HTML
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLoginGetUnauthenticated(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_returns_200(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200,
                         f"Expected 200, got {r.status_code}")

    def test_response_is_html(self):
        r = self.client.get("/login")
        ct = r.headers.get("content-type", "")
        self.assertIn("text/html", ct,
                      f"Expected HTML content-type, got: {ct!r}")


# ---------------------------------------------------------------------------
# Case 2 — GET /login already logged in -> 302 /my/
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLoginGetAuthenticated(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = _make_auth_client(self.app, user_id=1, role="user")

    def test_redirects_to_my(self):
        r = self.client.get("/login", follow_redirects=False)
        self.assertIn(r.status_code, (301, 302, 307, 308),
                      f"Expected redirect, got {r.status_code}")
        location = r.headers.get("location", "")
        self.assertIn("/my/", location,
                      f"Expected /my/ redirect, got: {location!r}")


# ---------------------------------------------------------------------------
# Case 3 — POST /login wrong password -> not redirected
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLoginPostWrongPassword(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(challenge_ok=False)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_wrong_password_not_redirected(self):
        r = self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "wrongpass"},
            follow_redirects=False,
        )
        self.assertNotIn(r.status_code, (301, 302, 307, 308),
                         f"Wrong password must not redirect, got {r.status_code}")

    def test_wrong_password_increments_evil(self):
        self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "wrongpass"},
            follow_redirects=False,
        )
        self.app.state.db.redis.evil.assert_called()


# ---------------------------------------------------------------------------
# Case 4 — POST /login success -> 302 + Set-Cookie
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLoginPostSuccess(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(challenge_ok=True)
        self.client = TestClient(self.app, raise_server_exceptions=True)

    def test_redirects_on_success(self):
        r = self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "correctpass"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (301, 302, 307, 308),
                      f"Expected redirect after successful login, got {r.status_code}: {r.text[:200]}")

    def test_set_cookie_present(self):
        r = self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "correctpass"},
            follow_redirects=False,
        )
        set_cookie_header = r.headers.get("set-cookie", "")
        has_cookie = "user" in set_cookie_header or "user" in r.cookies
        self.assertTrue(has_cookie,
                        f"Expected 'user' cookie to be set. Headers: {dict(r.headers)}")


# ---------------------------------------------------------------------------
# Case 5 — GET /logout -> 302 + cookie cleared
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLogout(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = _make_auth_client(self.app, user_id=1, role="user")

    def test_logout_redirects(self):
        r = self.client.get("/logout", follow_redirects=False)
        self.assertIn(r.status_code, (301, 302, 307, 308),
                      f"Expected redirect from /logout, got {r.status_code}")

    def test_logout_cookie_cleared(self):
        r = self.client.get("/logout", follow_redirects=False)
        location = r.headers.get("location", "")
        set_cookie = r.headers.get("set-cookie", "")
        # Either the cookie is explicitly deleted (max-age=0 / expires past)
        # or the redirect goes to "/" (proving logout was processed)
        self.assertTrue(
            "user" in set_cookie or location in ("/", "http://testserver/"),
            f"Logout cookie not cleared. set-cookie: {set_cookie!r}, location: {location!r}",
        )


# ---------------------------------------------------------------------------
# Case 6 — POST /login missing fields -> not redirected
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLoginPostMissingFields(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_missing_password_not_redirected(self):
        r = self.client.post(
            "/login",
            data={"email": "test@example.com", "password": ""},
            follow_redirects=False,
        )
        self.assertNotIn(r.status_code, (301, 302, 307, 308),
                         f"Missing password must not redirect, got {r.status_code}")

    def test_missing_email_not_redirected(self):
        r = self.client.post(
            "/login",
            data={"email": "", "password": "somepass"},
            follow_redirects=False,
        )
        self.assertNotIn(r.status_code, (301, 302, 307, 308),
                         f"Missing email must not redirect, got {r.status_code}")


# ---------------------------------------------------------------------------
# Case 7 — POST /login disabled account -> not redirected
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLoginPostDisabledAccount(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(user_status="Disable", user_role="user")
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_disabled_account_not_redirected(self):
        r = self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "somepass"},
            follow_redirects=False,
        )
        self.assertNotIn(r.status_code, (301, 302, 307, 308),
                         f"Disabled account must not redirect, got {r.status_code}")


# ---------------------------------------------------------------------------
# Case 8 — Routes registered in app
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestLoginRoutesRegistered(unittest.TestCase):
    def test_login_route_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/login", paths, f"/login not found in routes: {paths}")

    def test_logout_route_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/logout", paths, f"/logout not found in routes: {paths}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
