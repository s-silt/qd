#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI task run / log / setTime / task_multi handlers.

Tests:
  1.  GET  /task/{id}/log  own task → 200
  2.  GET  /task/{id}/log  other user's task → 401
  3.  POST /task/{id}/settime → update schedule (200 success page)
  4.  POST /task/{id}/run  → trigger run (202 accepted JSON)
  5.  POST /task/{userid}/multi?op=disable → batch disable (200 success page)
  6.  GET  /task/{id}/log  anonymous → 401
  7.  POST /task/{id}/disable → redirect 303
  8.  GET  /task/{id}/settime → 200 form
  9.  GET  /task/{id}/log/del → 303 redirect
 10.  POST /task/{userid}/get_tasksinfo → 200

Skipped automatically when fastapi / httpx are not installed.
"""

import json
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Conditional skip if fastapi/httpx are not installed
# ---------------------------------------------------------------------------

pytest = pytest_importorskip = None
try:
    import pytest as _pytest
    pytest = _pytest
    pytest_importorskip = _pytest.importorskip
except ImportError:
    pass

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed — skipping FastAPI Phase 2 task run tests"


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

_TASK_A = {
    "id": 1,
    "tplid": 10,
    "userid": 1,
    "disabled": 0,
    "note": "test task",
    "ontime": 0,
    "ontimeflg": 0,
    "newontime": json.dumps({"sw": False, "mode": "ontime", "time": "00:00:00", "date": "2026-01-01"}),
    "pushsw": json.dumps({}),
    "init_env": b"{}",
    "env": b"{}",
    "session": b"{}",
    "retry_count": 3,
    "retry_interval": None,
    "last_success": 0,
    "last_failed": 0,
    "success_count": 0,
    "failed_count": 0,
    "last_failed_count": 0,
    "next": 0,
}

_TASK_B = dict(_TASK_A, id=2, userid=2)

_TPL_A = {
    "id": 10,
    "userid": 1,
    "sitename": "Site A",
    "siteurl": "https://a.example",
    "tpl": b"[]",
    "interval": 86400,
    "last_success": 0,
}

_USER_A = {"id": 1, "role": "normal", "email": "a@example.com", "nickname": "UserA", "isadmin": False}
_USER_B = {"id": 2, "role": "normal", "email": "b@example.com", "nickname": "UserB", "isadmin": False}


def _make_mock_db():
    db = MagicMock()

    async def _task_get(taskid, fields=None, sql_session=None):
        mapping = {1: _TASK_A, "1": _TASK_A, 2: _TASK_B, "2": _TASK_B}
        task = mapping.get(int(taskid) if str(taskid).isdigit() else taskid)
        if task is None:
            return None
        if fields:
            return {k: v for k, v in task.items() if k in fields}
        return dict(task)

    async def _tpl_get(tplid, fields=None, sql_session=None):
        if int(tplid) == 10:
            tpl = _TPL_A
            if fields:
                return {k: v for k, v in tpl.items() if k in fields}
            return dict(tpl)
        return None

    db.task.get = AsyncMock(side_effect=_task_get)
    db.task.mod = AsyncMock()
    db.task.list = AsyncMock(return_value=[_TASK_A])
    db.task.delete = AsyncMock()

    db.tpl.get = AsyncMock(side_effect=_tpl_get)
    db.tpl.list = AsyncMock(return_value=[])
    db.tpl.incr_success = AsyncMock()

    db.tasklog.list = AsyncMock(return_value=[
        {"id": 1, "success": 1, "ctime": 0, "msg": "ok"},
        {"id": 2, "success": 0, "ctime": 0, "msg": "fail"},
    ])
    db.tasklog.add = AsyncMock(return_value=99)
    db.tasklog.delete = AsyncMock()

    db.site.get = AsyncMock(return_value={"logDay": 7})

    db.user.decrypt = AsyncMock(return_value={"_proxy": ""})
    db.user.encrypt = AsyncMock(return_value=b"{}")

    @asynccontextmanager
    async def _tx():
        yield None

    db.transaction = _tx

    db.redis = MagicMock()
    db.redis.is_evil = MagicMock(return_value=False)
    db.redis.evil = MagicMock()

    return db


def _make_mock_fetcher():
    fetcher = MagicMock()
    fetcher.do_fetch = AsyncMock(
        return_value=({"variables": {"__log__": "task ran ok"}}, None)
    )
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
class TestTaskLogHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 1: GET /task/1/log — own task → 200
    def test_task_log_own_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/task/1/log", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)

    # Case 2: GET /task/1/log — other user's task → 401
    def test_task_log_other_user_401(self):
        cookie = _make_user_cookie(_USER_B)
        resp = self.client.get("/task/1/log", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 401)

    # Case 6: GET /task/1/log — anonymous → 401
    def test_task_log_anonymous_401(self):
        resp = self.client.get("/task/1/log")
        self.assertEqual(resp.status_code, 401)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskSetTimeHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 8: GET /task/1/settime → 200 form
    def test_task_settime_get_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/task/1/settime", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)

    # Case 3: POST /task/1/settime → sets schedule → returns 200 success page
    def test_task_settime_post_sw_false_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post(
            "/task/1/settime",
            data={"sw": "false"},
            cookies={"user": cookie},
        )
        self.assertEqual(resp.status_code, 200)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskRunHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.fetcher = _make_mock_fetcher()
        self.app = _make_app(db=self.db, fetcher=self.fetcher)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 4: POST /task/1/run → accepted JSON response (does not need to actually run)
    def test_task_run_accepted(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post("/task/1/run", cookies={"user": cookie})
        # Expect 200 (JSON accepted) or 202
        self.assertIn(resp.status_code, (200, 202))
        body = resp.json()
        self.assertIn("taskid", body)
        self.assertEqual(body["taskid"], 1)

    # POST /task/1/run anonymous → 401
    def test_task_run_anonymous_401(self):
        resp = self.client.post("/task/1/run")
        self.assertEqual(resp.status_code, 401)

    # POST /task/1/run other user → 401
    def test_task_run_other_user_401(self):
        cookie = _make_user_cookie(_USER_B)
        resp = self.client.post("/task/1/run", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 401)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskDisableHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 7: POST /task/1/disable → 303
    def test_task_disable_post_303(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post(
            "/task/1/disable", cookies={"user": cookie}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskLogDelHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 9: GET /task/1/log/del → 303
    def test_task_log_del_get_303(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get(
            "/task/1/log/del", cookies={"user": cookie}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskMultiHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 5: POST /task/{userid}/multi?op=disable → 200
    def test_task_multi_disable_200(self):
        cookie = _make_user_cookie(_USER_A)
        payload = {
            "selectedtasks": json.dumps({"1": True}),
        }
        resp = self.client.post(
            "/task/1/multi?op=disable",
            data=payload,
            cookies={"user": cookie},
        )
        self.assertEqual(resp.status_code, 200)

    # Case 10: POST /task/{userid}/get_tasksinfo → 200
    def test_get_tasksinfo_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post(
            "/task/1/get_tasksinfo",
            data={"1": "true"},
            cookies={"user": cookie},
        )
        self.assertEqual(resp.status_code, 200)

    # GET /task/{userid}/multi?op=disable anonymous → 401
    def test_task_multi_get_anonymous_401(self):
        resp = self.client.get("/task/1/multi?op=disable")
        self.assertEqual(resp.status_code, 401)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTaskRouteDiscovery(unittest.TestCase):

    def test_task_routes_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/task/{taskid}/log", paths)
        self.assertIn("/task/{taskid}/run", paths)
        self.assertIn("/task/{taskid}/settime", paths)
        self.assertIn("/task/{taskid}/disable", paths)
        self.assertIn("/task/{userid}/multi", paths)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
