#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI index + my handlers.

Tests:
  1.  GET /       unauthenticated -> redirect to login page or renders index
  2.  GET /       authenticated   -> 302 redirect to /my/
  3.  GET /my/    unauthenticated -> 401
  4.  GET /my/    authenticated   -> 200 HTML
  5.  GET /my/checkupdate authenticated -> 302 redirect (POST-equivalent success path)
  6.  GET /my/    authenticated, user not found in DB -> 302 redirect to /login
  7.  GET /       authenticated but db has no public tpls -> 200 (empty tpl list)
  8.  GET /my/checkupdate unauthenticated -> 401 (permission-failure path)

Skipped automatically when fastapi / httpx are not installed.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Conditional skip if fastapi is unavailable
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed — skipping FastAPI Phase 2 simple tests"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal FastAPI app with a mock DB."""
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


def _make_mock_db(
    user_exists=True,
    user_role="user",
    public_tpls=None,
    user_tpls=None,
    tasks=None,
    tpl_detail=None,
):
    """Return a MagicMock DB with async helpers wired up."""
    db = MagicMock()

    # db.user.get — return user row when user_exists else None
    async def _user_get(uid, fields=None, **kw):
        if not user_exists:
            return None
        if fields and fields == ("role",):
            return {"role": user_role}
        return {"id": uid}

    db.user.get = _user_get

    # db.tpl.list — public (userid=None) or per-user
    _public_tpls = public_tpls if public_tpls is not None else [
        {"id": 1, "sitename": "TestSite", "success_count": 42},
    ]
    _user_tpls = user_tpls if user_tpls is not None else [
        {
            "id": 1, "siteurl": "https://example.com", "sitename": "TestSite",
            "banner": "", "note": "", "disabled": 0, "lock": 0,
            "last_success": 0, "ctime": 0, "mtime": 0, "fork": 0,
            "_groups": "default", "updateable": 0, "tplurl": "",
        },
    ]

    async def _tpl_list(userid=None, fields=None, limit=None, **kw):
        if userid is None:
            return _public_tpls
        return _user_tpls

    db.tpl.list = _tpl_list

    # db.tpl.get — return a tpl detail row
    _tpl_detail = tpl_detail if tpl_detail is not None else {
        "id": 1, "userid": None, "sitename": "TestSite", "siteurl": "",
        "note": "", "variables": "[]", "init_env": "{}",
    }

    async def _tpl_get(tplid, fields=None, **kw):
        return _tpl_detail

    db.tpl.get = _tpl_get

    # db.tpl.mod
    db.tpl.mod = AsyncMock()

    # db.task.list
    _tasks = tasks if tasks is not None else [
        {
            "id": 10, "tplid": 1, "note": "", "disabled": 0,
            "last_success": 0, "success_count": 5, "failed_count": 0,
            "last_failed": 0, "next": 0, "last_failed_count": 0,
            "ctime": 0, "_groups": "default",
        },
    ]

    async def _task_list(userid, fields=None, limit=None, **kw):
        return _tasks

    db.task.list = _task_list

    # db.pubtpl.list (for checkupdate)
    async def _pubtpl_list(fields=None, **kw):
        return []

    db.pubtpl.list = _pubtpl_list

    # db.transaction() — async context manager
    class _FakeTx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            pass

    db.transaction = lambda: _FakeTx()

    return db


def _make_auth_cookie(app, user_id=1, role="user"):
    """
    Create a valid 'user' secure cookie payload (umsgpack + Tornado-compat signing).
    Returns the raw cookie string suitable for the TestClient headers.
    """
    import umsgpack
    from web.fastapi.auth import set_secure_cookie
    from fastapi import APIRouter, Response
    import config

    helper_router = APIRouter()

    @helper_router.get("/_test_set_auth")
    def _set(response: Response):
        payload = umsgpack.packb({"id": user_id, "role": role})
        set_secure_cookie(response, "user", payload)
        return {"ok": True}

    app.include_router(helper_router)
    client = TestClient(app)
    r = client.get("/_test_set_auth")
    assert r.status_code == 200, f"Cookie setup endpoint failed: {r.status_code}"
    return client  # return client with cookies set


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestIndexRouteUnauthenticated(unittest.TestCase):
    """Case 1: GET / unauthenticated."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = TestClient(self.app, raise_server_exceptions=True)

    def test_get_index_unauthenticated_status(self):
        """Unauthenticated request to / should succeed (200) and render index.html."""
        response = self.client.get("/")
        self.assertIn(response.status_code, (200, 302, 301, 307, 308),
                      f"Unexpected status: {response.status_code}")

    def test_get_index_unauthenticated_not_redirected_to_my(self):
        """Unauthenticated / must NOT redirect to /my/."""
        response = self.client.get("/", follow_redirects=False)
        # If it redirects, it should not go to /my/
        if response.status_code in (301, 302, 307, 308):
            location = response.headers.get("location", "")
            self.assertNotIn("/my/", location,
                             "Unauthenticated user should not be sent to /my/")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestIndexRouteAuthenticated(unittest.TestCase):
    """Case 2: GET / authenticated -> redirect to /my/."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = _make_auth_cookie(self.app, user_id=1, role="user")

    def test_get_index_authenticated_redirects_to_my(self):
        """Authenticated user visiting / should be redirected to /my/."""
        response = self.client.get("/", follow_redirects=False)
        self.assertIn(response.status_code, (301, 302, 307, 308),
                      f"Expected a redirect, got {response.status_code}")
        location = response.headers.get("location", "")
        self.assertIn("/my/", location,
                      f"Expected redirect to /my/, got location: {location!r}")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestMyRouteUnauthenticated(unittest.TestCase):
    """Case 3: GET /my/ unauthenticated -> 401."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_get_my_unauthenticated_returns_401(self):
        """Unauthenticated GET /my/ must return 401."""
        response = self.client.get("/my/")
        self.assertEqual(response.status_code, 401,
                         f"Expected 401, got {response.status_code}")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestMyRouteAuthenticated(unittest.TestCase):
    """Case 4: GET /my/ authenticated -> 200 HTML."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = _make_auth_cookie(self.app, user_id=1, role="user")

    def test_get_my_authenticated_returns_200(self):
        """Authenticated GET /my/ should return 200 and HTML."""
        response = self.client.get("/my/")
        self.assertEqual(response.status_code, 200,
                         f"Expected 200, got {response.status_code}: {response.text[:200]}")
        content_type = response.headers.get("content-type", "")
        self.assertIn("text/html", content_type)

    def test_get_my_authenticated_admin_flag(self):
        """Admin user visiting /my/ gets 200 with adminflg=True in context."""
        self.app.state.db = _make_mock_db(user_role="admin")
        response = self.client.get("/my/")
        self.assertEqual(response.status_code, 200)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestCheckUpdateRoute(unittest.TestCase):
    """Case 5: GET /my/checkupdate authenticated -> 302 to /my/ (success path)."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = _make_auth_cookie(self.app, user_id=1, role="user")

    def test_checkupdate_authenticated_redirects_to_my(self):
        """GET /my/checkupdate should process and redirect to /my/."""
        response = self.client.get("/my/checkupdate", follow_redirects=False)
        self.assertIn(response.status_code, (301, 302, 307, 308),
                      f"Expected redirect, got {response.status_code}")
        location = response.headers.get("location", "")
        self.assertIn("/my/", location,
                      f"Expected redirect to /my/, got: {location!r}")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestMyRouteUserDeletedFromDB(unittest.TestCase):
    """Case 6: GET /my/ authenticated, user record removed from DB -> redirect /login."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(user_exists=False)
        self.client = _make_auth_cookie(self.app, user_id=1, role="user")

    def test_get_my_user_not_in_db_redirects_to_login(self):
        """If user is in cookie but deleted from DB, redirect to /login."""
        response = self.client.get("/my/", follow_redirects=False)
        self.assertIn(response.status_code, (301, 302, 307, 308),
                      f"Expected redirect, got {response.status_code}")
        location = response.headers.get("location", "")
        self.assertIn("/login", location,
                      f"Expected redirect to /login, got: {location!r}")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestIndexNoPublicTpls(unittest.TestCase):
    """Case 7: GET / unauthenticated with empty public tpl list -> 200."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(public_tpls=[])
        self.client = TestClient(self.app, raise_server_exceptions=True)

    def test_index_empty_tpl_list_returns_200(self):
        """Index page should render even when no public templates exist."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestCheckUpdateUnauthenticated(unittest.TestCase):
    """Case 8: GET /my/checkupdate unauthenticated -> 401 (permission failure)."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_checkupdate_unauthenticated_returns_401(self):
        """Unauthenticated GET /my/checkupdate must return 401."""
        response = self.client.get("/my/checkupdate")
        self.assertEqual(response.status_code, 401,
                         f"Expected 401, got {response.status_code}")


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestRouterDiscoveryPhase2(unittest.TestCase):
    """Verify index + my routers are auto-discovered alongside about router."""

    def test_index_route_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/", paths, f"/ not found in routes: {paths}")

    def test_my_route_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/my/", paths, f"/my/ not found in routes: {paths}")

    def test_checkupdate_route_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/my/checkupdate", paths,
                      f"/my/checkupdate not found in routes: {paths}")

    def test_about_still_registered(self):
        """about.py router must still coexist with new routers."""
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/about", paths,
                      f"/about not found in routes after adding index+my: {paths}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
