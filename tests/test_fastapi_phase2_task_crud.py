#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI port of task CRUD endpoints.

Tests:
  1.  POST /task/new  — authenticated → creates task, redirects (303)
  2.  POST /task/new  — anonymous → 401
  3.  POST /task/{id}/del  — own task → 303 redirect
  4.  POST /task/{id}/del  — other user's task → 401
  5.  POST /task/{id}/setgroup  — change group → 303 redirect
  6.  GET  /task/new  — form render → 200
  7.  GET  /task/{id}/edit  — owner → 200
  8.  GET  /task/{id}/edit  — non-owner → 401
  9.  POST /task/{id}/edit  — owner saves → 303
 10.  GET  /task/{id}/var   — owner → 200

These tests use MagicMock DB/Fetcher objects so they run without a real database.
Skipped automatically when fastapi / httpx are not installed.

Note: POST requests use raw URL-encoded body (`content=b"..."`) to avoid
requiring python-multipart, which may not be installed in the test environment.
The task handler includes a fallback parser for this case.
"""

import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Conditional skip
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient  # noqa: F401
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed — skipping FastAPI Phase 2 task CRUD tests"

_FORM_CT = "application/x-www-form-urlencoded"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_USER_A = {"id": 1, "role": "normal", "email": "a@example.com", "nickname": "UserA", "isadmin": False}
_USER_B = {"id": 2, "role": "normal", "email": "b@example.com", "nickname": "UserB", "isadmin": False}

# Task owned by USER_A
_TASK_A = {
    "id": 101,
    "userid": 1,
    "tplid": 10,
    "disabled": 0,
    "note": "task note",
    "retry_count": 3,
    "retry_interval": None,
    "init_env": b"encrypted",
    "env": b"",
    "session": b"",
    "last_success": 0,
    "last_failed": 0,
    "success_count": 5,
    "failed_count": 0,
    "last_failed_count": 0,
    "next": 0,
    "ontime": 0,
    "ontimeflg": 0,
    "pushsw": "{}",
    "newontime": "{}",
    "_groups": "default",
}

# Template owned by USER_A
_TPL_A = {
    "id": 10,
    "userid": 1,
    "sitename": "Site A",
    "siteurl": "https://a.example",
    "public": 0,
    "note": "note",
    "banner": "",
    "tpl": b"tpl",
    "variables": '["var1", "var2"]',
    "init_env": '{"var1": "v1"}',
    "disabled": 0,
    "lock": False,
    "last_success": 0,
    "ctime": 0,
    "mtime": 0,
    "fork": None,
    "success_count": 5,
    "_groups": "default",
    "interval": 86400,
}


def _make_mock_db():
    """Build a minimal mock DB that satisfies the task CRUD handler calls."""
    db = MagicMock()

    # --- tpl table ---
    async def _tpl_get(tplid, fields=None, sql_session=None):
        tpl = _TPL_A if int(tplid) == 10 else None
        if tpl is None:
            return None
        if fields:
            return {k: v for k, v in tpl.items() if k in fields}
        return dict(tpl)

    db.tpl.get = AsyncMock(side_effect=_tpl_get)
    db.tpl.list = AsyncMock(return_value=[_TPL_A])
    db.tpl.mod = AsyncMock()
    db.tpl.delete = AsyncMock()
    db.tpl.add = AsyncMock(return_value=10)
    db.tpl.incr_success = AsyncMock()

    # --- task table ---
    async def _task_get(taskid, fields=None, sql_session=None):
        task = _TASK_A if int(taskid) == 101 else None
        if task is None:
            return None
        if fields:
            return {k: v for k, v in task.items() if k in fields}
        return dict(task)

    db.task.get = AsyncMock(side_effect=_task_get)
    db.task.list = AsyncMock(return_value=[_TASK_A])
    db.task.add = AsyncMock(return_value=101)
    db.task.mod = AsyncMock()
    db.task.delete = AsyncMock()

    # --- tasklog table ---
    db.tasklog.list = AsyncMock(return_value=[])
    db.tasklog.delete = AsyncMock()
    db.tasklog.add = AsyncMock()

    # --- user table ---
    db.user.get = AsyncMock(return_value={
        "id": 1, "role": "normal", "email": "a@example.com",
        "email_verified": 1, "nickname": "UserA",
    })
    db.user.decrypt = AsyncMock(return_value={"var1": "v1", "_proxy": ""})
    db.user.encrypt = AsyncMock(return_value=b"encrypted")

    # --- site table ---
    db.site.get = AsyncMock(return_value={"regEn": 1, "MustVerifyEmailEn": 0, "logDay": 7})

    # --- transaction context manager ---
    @asynccontextmanager
    async def _tx():
        yield None

    db.transaction = _tx

    # --- redis ---
    db.redis = MagicMock()
    db.redis.is_evil = MagicMock(return_value=False)
    db.redis.evil = MagicMock()

    return db


def _make_mock_fetcher():
    fetcher = MagicMock()
    fetcher.do_fetch = AsyncMock(return_value=({"variables": {"__log__": "ok"}}, None))
    return fetcher


def _make_app(db=None, fetcher=None):
    from web.fastapi_app import create_app
    return create_app(
        db=db or _make_mock_db(),
        fetcher=fetcher or _make_mock_fetcher(),
        version="test",
    )


def _make_user_cookie(user_dict: dict) -> str:
    import umsgpack
    from web.fastapi.auth import create_signed_value
    import config
    raw = umsgpack.packb(user_dict)
    return create_signed_value("user", raw, secret=config.cookie_secret)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskCRUD(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.cookie_a = _make_user_cookie(_USER_A)
        self.cookie_b = _make_user_cookie(_USER_B)

    # Case 1: POST /task/new — authenticated → 303 redirect (task created)
    def test_task_new_post_authenticated_redirects(self):
        resp = self.client.post(
            "/task/new",
            content=b"_binux_tplid=10&_binux_note=My+Task&_binux_proxy=&_binux_retry_count=3&_binux_retry_interval=",
            headers={"Content-Type": _FORM_CT},
            cookies={"user": self.cookie_a},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.db.task.add.assert_called_once()

    # Case 2: POST /task/new — anonymous → 401
    def test_task_new_post_anonymous_401(self):
        resp = self.client.post(
            "/task/new",
            content=b"_binux_tplid=10&_binux_note=Task",
            headers={"Content-Type": _FORM_CT},
        )
        self.assertEqual(resp.status_code, 401)

    # Case 3: POST /task/{id}/del — own task → 303 redirect
    def test_task_del_own_task_303(self):
        resp = self.client.post(
            "/task/101/del",
            cookies={"user": self.cookie_a},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.db.task.delete.assert_called_once()

    # Case 4: POST /task/{id}/del — other user's task → 401
    def test_task_del_other_user_401(self):
        resp = self.client.post(
            "/task/101/del",
            cookies={"user": self.cookie_b},
        )
        self.assertEqual(resp.status_code, 401)

    # Case 5: POST /task/{id}/setgroup — change group → 303 redirect
    def test_task_setgroup_post_303(self):
        resp = self.client.post(
            "/task/101/setgroup",
            content=b"New_group=mygroup",
            headers={"Content-Type": _FORM_CT},
            cookies={"user": self.cookie_a},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.db.task.mod.assert_called()

    # Case 6: GET /task/new — form render → 200
    def test_task_new_get_200(self):
        resp = self.client.get("/task/new", cookies={"user": self.cookie_a})
        self.assertEqual(resp.status_code, 200)

    # Case 7: GET /task/{id}/edit — owner → 200
    def test_task_edit_get_owner_200(self):
        resp = self.client.get("/task/101/edit", cookies={"user": self.cookie_a})
        self.assertEqual(resp.status_code, 200)

    # Case 8: GET /task/{id}/edit — non-owner → 401
    def test_task_edit_get_other_user_401(self):
        resp = self.client.get("/task/101/edit", cookies={"user": self.cookie_b})
        self.assertEqual(resp.status_code, 401)

    # Case 9: POST /task/{id}/edit — owner saves → 303
    def test_task_edit_post_owner_303(self):
        resp = self.client.post(
            "/task/101/edit",
            content=b"_binux_note=Updated+Task&_binux_proxy=&_binux_retry_count=3&_binux_retry_interval=",
            headers={"Content-Type": _FORM_CT},
            cookies={"user": self.cookie_a},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

    # Case 10: GET /task/{id}/var — owner → 200
    def test_task_var_get_owner_200(self):
        resp = self.client.get("/task/101/var", cookies={"user": self.cookie_a})
        self.assertEqual(resp.status_code, 200)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskRouteDiscovery(unittest.TestCase):

    def test_task_routes_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/task/new", paths)
        self.assertIn("/task/{taskid}/edit", paths)
        self.assertIn("/task/{taskid}/del", paths)
        self.assertIn("/task/{taskid}/var", paths)
        self.assertIn("/task/{taskid}/setgroup", paths)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
