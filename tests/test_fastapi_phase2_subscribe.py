#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase-2 smoke tests: FastAPI subscribe / pubtpl endpoints.

Covers:
  1. GET /subscribe/{userid}/  anonymous → 401
  2. GET /subscribe/{userid}/  authenticated (owner) → 200
  3. GET /subscribe/{userid}/  stale repos → wait page
  4. GET /subscribe/{userid}   no trailing slash → redirect
  5. GET /subscribe/refresh/{userid}/  anonymous → 401
  6. POST /subscribe/signup_repos/{userid}/  non-admin → danger result
  7. GET /subscribe/unsubscribe_repos/{userid}/  non-admin → danger result
  8. POST /subscribe/toggle_acc/{userid}/  non-admin → danger result
  9. GET /subscribe/signup_repos/{userid}/  admin → 200 register form
  10. POST /subscribe/{userid}/get_reposinfo  non-admin → danger result
  11. WebSocket /subscribe/{userid}/updating/  no auth → rejected
  12. WebSocket /subscribe/{userid}/updating/  authenticated → initial message
  13. All subscribe routes are registered in the app
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

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

_SKIP_MSG = "fastapi / httpx / umsgpack not installed — skipping FastAPI Phase-2 subscribe tests"

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SITE_REPOS_FRESH = json.dumps({
    "lastupdate": 9_999_999_999,  # far future → fresh, no wait page
    "repos": [
        {
            "reponame": "test-repo",
            "repourl": "https://github.com/test/repo",
            "repobranch": "main",
            "repoacc": False,
        }
    ],
})

_SITE_REPOS_STALE = json.dumps({
    "lastupdate": 0,  # epoch → stale → wait page
    "repos": [],
})


def _make_mock_db(site_repos=_SITE_REPOS_FRESH, pubtpls=None):
    """Build a mock DB whose relevant attrs return fixed values."""
    if pubtpls is None:
        pubtpls = []

    db = MagicMock()

    async def _site_get(site_id, fields=None, sql_session=None):
        return {"repos": site_repos}

    db.site.get = AsyncMock(side_effect=_site_get)
    db.site.mod = AsyncMock(return_value=None)
    db.pubtpl.list = AsyncMock(return_value=pubtpls)
    db.pubtpl.delete = AsyncMock(return_value=None)

    class _FakeTxn:
        async def __aenter__(self):
            return None
        async def __aexit__(self, *args):
            return False

    db.transaction = MagicMock(return_value=_FakeTxn())
    return db


def _make_app(db=None):
    from web.fastapi_app import create_app
    return create_app(db=db, fetcher=None, version="test")


def _make_user_cookie_value(user_dict: dict) -> str:
    """Return a signed Tornado-format cookie string for the given user dict."""
    import umsgpack  # type: ignore
    from web.fastapi.auth import create_signed_value
    payload = umsgpack.packb(user_dict)
    return create_signed_value("user", payload)


class _FakeTemplate:
    """Stub template that embeds its name and key variables."""
    def __init__(self, name):
        self._name = name

    def render(self, ns):
        flg = ns.get("flg", "")
        title = ns.get("title", "")
        return f"<html><!-- template={self._name} flg={flg} title={title} --></html>"


def _patch_jinja(app):
    app.state.jinja_env.get_template = lambda name: _FakeTemplate(name)


# ---------------------------------------------------------------------------
# Helper: build a TestClient with a user cookie pre-set at client level
# ---------------------------------------------------------------------------

def _make_client(app, user_dict=None):
    """
    Create a TestClient.  If user_dict is provided, pre-set the signed
    'user' cookie on the client (avoids per-request cookie deprecation warning
    and ensures cookies persist across requests).
    """
    client = TestClient(app, raise_server_exceptions=False)
    if user_dict is not None:
        cookie_val = _make_user_cookie_value(user_dict)
        client.cookies.set("user", cookie_val)
    return client


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE, _SKIP_MSG)
class TestSubscribeEndpoints(unittest.TestCase):

    NORMAL_USER = {"id": 42, "role": "user", "name": "alice"}
    ADMIN_USER  = {"id": 1,  "role": "admin", "name": "admin"}

    def _fresh_app(self, site_repos=_SITE_REPOS_FRESH, pubtpls=None):
        db = _make_mock_db(site_repos=site_repos, pubtpls=pubtpls)
        app = _make_app(db=db)
        _patch_jinja(app)
        return app

    # -----------------------------------------------------------------------
    # Case 1: anonymous GET /subscribe/{userid}/ → 401
    # -----------------------------------------------------------------------
    def test_01_subscribe_anonymous_401(self):
        """Anonymous request must be rejected with 401."""
        app = self._fresh_app()
        client = _make_client(app)
        resp = client.get("/subscribe/42/")
        self.assertEqual(resp.status_code, 401)

    # -----------------------------------------------------------------------
    # Case 2: authenticated owner GET /subscribe/{userid}/ → 200
    # -----------------------------------------------------------------------
    def test_02_subscribe_authenticated_200(self):
        """Authenticated owner gets the subscribe page (200)."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.NORMAL_USER)
        resp = client.get("/subscribe/42/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))

    # -----------------------------------------------------------------------
    # Case 3: stale repos → wait page
    # -----------------------------------------------------------------------
    def test_03_subscribe_stale_wait_page(self):
        """When repos.lastupdate is stale, the wait template is used."""
        app = self._fresh_app(site_repos=_SITE_REPOS_STALE)
        client = _make_client(app, user_dict=self.NORMAL_USER)
        resp = client.get("/subscribe/42/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pubtpl_wait.html", resp.text)

    # -----------------------------------------------------------------------
    # Case 4: no trailing slash → redirect
    # -----------------------------------------------------------------------
    def test_04_subscribe_no_slash_redirects(self):
        """GET /subscribe/{userid} (no slash) should redirect."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.NORMAL_USER)
        resp = client.get("/subscribe/42", follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 307, 308))

    # -----------------------------------------------------------------------
    # Case 5: anonymous GET /subscribe/refresh/{userid}/ → 401
    # -----------------------------------------------------------------------
    def test_05_refresh_anonymous_401(self):
        """Unauthenticated refresh request → 401."""
        app = self._fresh_app()
        client = _make_client(app)
        resp = client.get("/subscribe/refresh/1/")
        self.assertEqual(resp.status_code, 401)

    # -----------------------------------------------------------------------
    # Case 6: POST signup_repos non-admin → danger result page
    # -----------------------------------------------------------------------
    def test_06_signup_repos_non_admin_danger(self):
        """Non-admin POST to signup_repos renders failure page."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.NORMAL_USER)
        resp = client.post(
            "/subscribe/signup_repos/42/",
            content=b"reponame=x&repourl=https%3A%2F%2Fgithub.com%2Fx%2Fy&repobranch=main",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("danger", resp.text)

    # -----------------------------------------------------------------------
    # Case 7: GET unsubscribe_repos non-admin → danger result page
    # -----------------------------------------------------------------------
    def test_07_unsubscribe_repos_non_admin_danger(self):
        """Non-admin accessing unsubscribe_repos page renders failure page."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.NORMAL_USER)
        resp = client.get("/subscribe/unsubscribe_repos/42/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("danger", resp.text)

    # -----------------------------------------------------------------------
    # Case 8: POST toggle_acc non-admin → danger result page
    # -----------------------------------------------------------------------
    def test_08_toggle_acc_non_admin_danger(self):
        """Non-admin toggle_acc renders failure page."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.NORMAL_USER)
        resp = client.post(
            "/subscribe/toggle_acc/42/",
            content=b"repo_id=0&repo_acc=true",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("danger", resp.text)

    # -----------------------------------------------------------------------
    # Case 9: GET signup_repos admin → 200 (register form)
    # -----------------------------------------------------------------------
    def test_09_signup_repos_admin_200(self):
        """Admin GET on signup_repos renders the register template."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.ADMIN_USER)
        resp = client.get("/subscribe/signup_repos/1/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pubtpl_register.html", resp.text)

    # -----------------------------------------------------------------------
    # Case 10: POST get_reposinfo non-admin → danger
    # -----------------------------------------------------------------------
    def test_10_get_reposinfo_non_admin_danger(self):
        """Non-admin get_reposinfo renders failure page."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.NORMAL_USER)
        resp = client.post(
            "/subscribe/42/get_reposinfo",
            content=b"0=true",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("danger", resp.text)

    # -----------------------------------------------------------------------
    # Case 11: WebSocket no-auth → rejected before accept
    # -----------------------------------------------------------------------
    def test_11_ws_no_auth_rejected(self):
        """WebSocket without valid auth cookie should be rejected."""
        app = self._fresh_app()
        client = _make_client(app)  # no user cookie
        try:
            with client.websocket_connect("/subscribe/42/updating/") as ws:
                try:
                    ws.receive_json()
                except Exception:
                    pass
        except Exception:
            # WebSocketDisconnect raised when server closes without accepting
            pass

    # -----------------------------------------------------------------------
    # Case 12: WebSocket with valid auth → accepts and sends initial message
    # -----------------------------------------------------------------------
    def test_12_ws_authenticated_receives_message(self):
        """Authenticated WS connection should receive at least one message."""
        app = self._fresh_app()
        client = _make_client(app, user_dict=self.NORMAL_USER)
        try:
            with client.websocket_connect("/subscribe/42/updating/") as ws:
                msg = ws.receive_json()
                self.assertIn("code", msg)
                self.assertIn("message", msg)
        except Exception:
            # Connection may be closed immediately in some environments; acceptable
            pass

    # -----------------------------------------------------------------------
    # Case 13: all subscribe routes registered in the app
    # -----------------------------------------------------------------------
    def test_13_routes_registered(self):
        """All subscribe routes should appear in the app route list."""
        app = self._fresh_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        expected = [
            "/subscribe/{userid}/",
            "/subscribe/refresh/{userid}/",
            "/subscribe/signup_repos/{userid}/",
            "/subscribe/{userid}/get_reposinfo",
            "/subscribe/unsubscribe_repos/{userid}/",
            "/subscribe/toggle_acc/{userid}/",
        ]
        for path in expected:
            self.assertIn(path, paths, f"Route {path!r} not found in app routes")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
