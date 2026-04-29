#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase-2 smoke tests for FastAPI user management (admin) endpoints.

Cases:
  1. GET /user/manage anonymous          -> 401
  2. GET /user/manage regular user       -> 403
  3. GET /user/manage admin user         -> 200
  4. POST /user/manage/ban anonymous     -> 401
  5. POST /user/manage/ban admin         -> 200
  6. POST /user/manage/activate admin    -> 200
  7. POST /user/manage/verify admin      -> 200
  8. POST /user/manage/delete admin      -> 200
  9. All user/manage routes are registered

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

_SKIP_MSG = (
    "fastapi / httpx / umsgpack not installed -- skipping FastAPI Phase-2 user mgmt tests"
)
_SKIP = not (_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal FastAPI app (no real DB needed at startup)."""
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


def _make_mock_db(challenge_ok=True):
    """Return a MagicMock DB suitable for user-management flow."""
    db = MagicMock()

    # db.user.list -- returns a list of user dicts
    async def _user_list(fields=None, sql_session=None, **kw):
        row = {
            "id": 2,
            "email": "other@example.com",
            "role": "user",
            "status": "Enable",
            "ctime": 0,
            "atime": 0,
            "email_verified": 1,
            "aip": "127.0.0.1",
        }
        if fields:
            return [{k: row[k] for k in fields if k in row}]
        return [row]

    db.user.list = _user_list

    # db.user.get -- identity lookup
    async def _user_get(uid_or_kw=None, email=None, fields=None, sql_session=None, **kw):
        row = {
            "id": 2,
            "email": "other@example.com",
            "role": "user",
            "status": "Enable",
            "ctime": 0,
            "atime": 0,
            "email_verified": 1,
            "aip": "127.0.0.1",
        }
        if fields:
            return {k: row[k] for k in fields if k in row}
        return row

    db.user.get = _user_get

    # db.user.challenge_md5 -- admin password verification
    async def _challenge_md5(email, pwd, sql_session=None):
        return challenge_ok

    db.user.challenge_md5 = _challenge_md5

    # db.user.mod, db.user.delete -- mutations
    db.user.mod = AsyncMock(return_value=None)
    db.user.delete = AsyncMock(return_value=None)

    # db.task.list -- returns an empty list by default
    async def _task_list(userid=None, fields=None, limit=None, sql_session=None, **kw):
        return []

    db.task.list = _task_list
    db.task.mod = AsyncMock(return_value=None)
    db.task.delete = AsyncMock(return_value=None)

    # db.tasklog.list / delete
    async def _tasklog_list(taskid=None, fields=None, sql_session=None, **kw):
        return []

    db.tasklog.list = _tasklog_list
    db.tasklog.delete = AsyncMock(return_value=None)

    # db.tpl.list / delete
    async def _tpl_list(fields=None, limit=None, sql_session=None, **kw):
        return []

    db.tpl.list = _tpl_list
    db.tpl.delete = AsyncMock(return_value=None)

    # db.notepad.list / delete
    async def _notepad_list(fields=None, limit=None, userid=None, sql_session=None, **kw):
        return []

    db.notepad.list = _notepad_list
    db.notepad.delete = AsyncMock(return_value=None)

    # db.transaction() -- async context manager
    class _FakeTx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            pass

    db.transaction = lambda: _FakeTx()

    # db.redis -- evil counter (best-effort)
    db.redis = MagicMock()
    db.redis.evil = MagicMock(return_value=None)

    return db


def _make_auth_cookie(app, user_id=1, role="user"):
    """
    Create a TestClient with a valid 'user' secure cookie.
    Returns the client with cookies already stored.
    """
    import umsgpack
    from fastapi import APIRouter, Response
    from web.fastapi.auth import set_secure_cookie

    helper_router = APIRouter()

    @helper_router.get("/_test_set_usermgmt_auth")
    def _set(response: Response):
        payload = umsgpack.packb({
            "id": user_id,
            "role": role,
            "email": "admin@example.com" if role == "admin" else "user@example.com",
            "nickname": "tester",
            "email_verified": 1,
        })
        set_secure_cookie(response, "user", payload)
        return {"ok": True}

    app.include_router(helper_router)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/_test_set_usermgmt_auth")
    assert r.status_code == 200, f"Cookie setup failed: {r.status_code}"
    return client


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageGetAnonymous(unittest.TestCase):
    """Case 1: GET /user/manage anonymous -> 401."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_anonymous_returns_401(self):
        response = self.client.get("/user/manage")
        self.assertEqual(
            response.status_code,
            401,
            f"Expected 401 for anonymous, got {response.status_code}",
        )


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageGetRegularUser(unittest.TestCase):
    """Case 2: GET /user/manage regular user -> 403."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = _make_auth_cookie(self.app, user_id=2, role="user")

    def test_regular_user_returns_403(self):
        response = self.client.get("/user/manage")
        self.assertEqual(
            response.status_code,
            403,
            f"Expected 403 for regular user, got {response.status_code}",
        )


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageGetAdmin(unittest.TestCase):
    """Case 3: GET /user/manage admin -> 200."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = _make_auth_cookie(self.app, user_id=1, role="admin")

    def test_admin_returns_200(self):
        response = self.client.get("/user/manage")
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for admin, got {response.status_code}: {response.text[:200]}",
        )

    def test_response_is_html(self):
        response = self.client.get("/user/manage")
        ct = response.headers.get("content-type", "")
        self.assertIn("text/html", ct, f"Expected HTML, got content-type: {ct!r}")


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageBanAnonymous(unittest.TestCase):
    """Case 4: POST /user/manage/ban anonymous -> 401."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_anonymous_ban_returns_401(self):
        response = self.client.post(
            "/user/manage/ban",
            json={"user_ids": [2], "adminmail": "admin@example.com", "adminpwd": "secret"},
        )
        self.assertEqual(
            response.status_code,
            401,
            f"Expected 401 for anonymous, got {response.status_code}",
        )


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageBanAdmin(unittest.TestCase):
    """Case 5: POST /user/manage/ban admin -> 200."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(challenge_ok=True)
        self.client = _make_auth_cookie(self.app, user_id=1, role="admin")

    def test_admin_ban_returns_200(self):
        response = self.client.post(
            "/user/manage/ban",
            json={"user_ids": [2], "adminmail": "admin@example.com", "adminpwd": "secret"},
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for admin ban, got {response.status_code}: {response.text[:200]}",
        )


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageActivateAdmin(unittest.TestCase):
    """Case 6: POST /user/manage/activate admin -> 200."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(challenge_ok=True)
        self.client = _make_auth_cookie(self.app, user_id=1, role="admin")

    def test_admin_activate_returns_200(self):
        response = self.client.post(
            "/user/manage/activate",
            json={"user_ids": [2], "adminmail": "admin@example.com", "adminpwd": "secret"},
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for admin activate, got {response.status_code}: {response.text[:200]}",
        )


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageVerifyAdmin(unittest.TestCase):
    """Case 7: POST /user/manage/verify admin -> 200."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(challenge_ok=True)
        self.client = _make_auth_cookie(self.app, user_id=1, role="admin")

    def test_admin_verify_returns_200(self):
        response = self.client.post(
            "/user/manage/verify",
            json={"user_ids": [2], "adminmail": "admin@example.com", "adminpwd": "secret"},
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for admin verify, got {response.status_code}: {response.text[:200]}",
        )


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageDeleteAdmin(unittest.TestCase):
    """Case 8: POST /user/manage/delete admin -> 200."""

    def setUp(self):
        self.app = _make_app()
        self.app.state.db = _make_mock_db(challenge_ok=True)
        self.client = _make_auth_cookie(self.app, user_id=1, role="admin")

    def test_admin_delete_returns_200(self):
        response = self.client.post(
            "/user/manage/delete",
            json={"user_ids": [2], "adminmail": "admin@example.com", "adminpwd": "secret"},
        )
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 for admin delete, got {response.status_code}: {response.text[:200]}",
        )


@unittest.skipIf(_SKIP, _SKIP_MSG)
class TestUserManageRoutesRegistered(unittest.TestCase):
    """Case 9: Verify user management routes are registered."""

    def test_routes_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/user/manage", paths,
                      f"/user/manage not found in routes: {paths}")
        self.assertIn("/user/manage/ban", paths,
                      f"/user/manage/ban not found in routes: {paths}")
        self.assertIn("/user/manage/activate", paths,
                      f"/user/manage/activate not found in routes: {paths}")
        self.assertIn("/user/manage/verify", paths,
                      f"/user/manage/verify not found in routes: {paths}")
        self.assertIn("/user/manage/delete", paths,
                      f"/user/manage/delete not found in routes: {paths}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
