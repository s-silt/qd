#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI port of HAR editor / test / save handlers.

Tests:
  1. GET /har/edit  anonymous -> 200  (no auth required for the editor page)
  2. GET /tpl/{id}/edit  own template -> 200  (page renders)
  3. POST /tpl/{id}/edit  anonymous -> 401  (login required)
  4. POST /tpl/{id}/edit  own template -> 200 (returns template JSON)
  5. POST /har/test  single GET request -> 200  (mock fetcher)
  6. POST /har/save  logged-in -> 200  (creates new tpl, returns id)
  7. POST /tpl/{id}/save  logged-in owner -> 200  (updates existing tpl)
  8. POST /tpl/{id}/save  locked tpl -> 403

These tests use MagicMock DB/Fetcher objects so they run without a real database.
Skipped automatically when fastapi / httpx are not installed.
"""

import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed -- skipping FastAPI Phase 2 HAR editor tests"


def _make_mock_db():
    db = MagicMock()

    _tpl_a = {
        "id": 10, "userid": 1, "sitename": "Site A", "siteurl": "https://a.example",
        "note": "note", "banner": "", "har": b"encrypted_har",
        "variables": '["user", "pass"]', "init_env": '{"user": "admin"}',
        "lock": False, "interval": 86400, "_groups": "default",
    }
    _tpl_locked = {
        "id": 20, "userid": 1, "sitename": "Locked", "siteurl": "",
        "note": "", "banner": "", "har": b"enc",
        "variables": "[]", "init_env": "{}", "lock": True, "interval": None,
        "_groups": "default",
    }

    async def _tpl_get(tplid, fields=None, sql_session=None):
        tpl_map = {10: _tpl_a, 20: _tpl_locked}
        tpl = tpl_map.get(int(tplid))
        if tpl is None:
            return None
        if fields:
            return {k: v for k, v in tpl.items() if k in fields}
        return dict(tpl)

    db.tpl.get = AsyncMock(side_effect=_tpl_get)
    db.tpl.mod = AsyncMock()
    db.tpl.add = AsyncMock(return_value=99)
    db.user.decrypt = AsyncMock(return_value=[])
    db.user.encrypt = AsyncMock(return_value=b"encrypted")
    db.pubtpl = MagicMock()
    db.pubtpl.list = AsyncMock(return_value=[])
    db.task = MagicMock()
    db.task.get = AsyncMock(return_value={"init_env": None})

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
    mock_env_session = MagicMock()
    mock_env_session.to_json = MagicMock(return_value=[])
    fetcher.fetch = AsyncMock(return_value={
        "success": True,
        "response": MagicMock(),
        "env": {"variables": {}, "session": mock_env_session},
    })
    fetcher.response2har = MagicMock(return_value={
        "request": {"method": "GET", "url": "https://example.com/", "headers": [], "cookies": []},
        "response": {"status": 200, "statusText": "OK", "headers": [],
                     "content": {"mimeType": "text/html", "size": 16}},
    })
    return fetcher


def _make_app(db=None, fetcher=None):
    from web.fastapi_app import create_app
    return create_app(
        db=db or _make_mock_db(),
        fetcher=fetcher or _make_mock_fetcher(),
        version="test",
    )


def _make_user_cookie(user_dict):
    import umsgpack
    from web.fastapi.auth import create_signed_value
    import config
    raw = umsgpack.packb(user_dict)
    return create_signed_value("user", raw, secret=config.cookie_secret)


_USER_A = {"id": 1, "role": "normal", "email": "a@example.com", "nickname": "UserA", "isadmin": False}
_USER_B = {"id": 2, "role": "normal", "email": "b@example.com", "nickname": "UserB", "isadmin": False}

_MINIMAL_SAVE_PAYLOAD = json.dumps({
    "har": [{"request": {"method": "GET", "url": "https://httpbin.org/get",
                         "headers": [], "cookies": []},
             "rule": {"find_all": False, "extract_variables": []}}],
    "tpl": [{"request": {"method": "GET", "url": "https://httpbin.org/get",
                         "headers": [], "cookies": [], "data": ""},
             "rule": {"find_all": False, "extract_variables": []}}],
    "setting": {"sitename": "Test Site", "siteurl": "https://httpbin.org",
                "note": "auto-test", "interval": 86400},
}).encode()

_SINGLE_REQUEST_PAYLOAD = json.dumps({
    "request": {"method": "GET", "url": "https://httpbin.org/get",
                "headers": [], "cookies": [], "data": ""},
    "rule": {"find_all": False, "extract_variables": []},
    "env": {"variables": {}, "session": []},
}).encode()


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestHAREditorGet(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_get_har_edit_anonymous_200(self):
        """Case 1: GET /har/edit anonymous -> 200."""
        resp = self.client.get("/har/edit")
        self.assertEqual(resp.status_code, 200)

    def test_get_tpl_edit_logged_in_200(self):
        """Case 2: GET /tpl/{id}/edit logged-in owner -> 200."""
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/tpl/10/edit", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestHAREditorPost(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_post_tpl_edit_anonymous_401(self):
        """Case 3: POST /tpl/{id}/edit anonymous -> 401."""
        resp = self.client.post("/tpl/10/edit")
        self.assertEqual(resp.status_code, 401)

    def test_post_tpl_edit_owner_200(self):
        """Case 4: POST /tpl/{id}/edit own template -> 200."""
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post("/tpl/10/edit", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("har", body)
        self.assertIn("setting", body)
        self.assertIn("readonly", body)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestHARTest(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.fetcher = _make_mock_fetcher()
        self.app = _make_app(db=self.db, fetcher=self.fetcher)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_post_har_test_single_get_200(self):
        """Case 5: POST /har/test with single GET request -> 200."""
        resp = self.client.post(
            "/har/test",
            content=_SINGLE_REQUEST_PAYLOAD,
            headers={"content-type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("success", body)
        self.assertIn("har", body)
        self.assertIn("env", body)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestHARSave(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_post_har_save_anonymous_401(self):
        """POST /har/save anonymous -> 401."""
        resp = self.client.post(
            "/har/save",
            content=_MINIMAL_SAVE_PAYLOAD,
            headers={"content-type": "application/json"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_post_har_save_logged_in_200(self):
        """Case 6: POST /har/save logged-in -> 200, returns new tpl id."""
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post(
            "/har/save",
            content=_MINIMAL_SAVE_PAYLOAD,
            headers={"content-type": "application/json"},
            cookies={"user": cookie},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("id", resp.json())

    def test_post_tpl_save_owner_200(self):
        """Case 7: POST /tpl/{id}/save owner -> 200."""
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post(
            "/tpl/10/save",
            content=_MINIMAL_SAVE_PAYLOAD,
            headers={"content-type": "application/json"},
            cookies={"user": cookie},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("id", resp.json())

    def test_post_tpl_save_locked_403(self):
        """Case 8: POST /tpl/{id}/save locked tpl -> 403."""
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post(
            "/tpl/20/save",
            content=_MINIMAL_SAVE_PAYLOAD,
            headers={"content-type": "application/json"},
            cookies={"user": cookie},
        )
        self.assertEqual(resp.status_code, 403)


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestHAREditorRouteDiscovery(unittest.TestCase):

    def test_har_editor_routes_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/har/edit", paths)
        self.assertIn("/tpl/{tplid}/edit", paths)
        self.assertIn("/har/test", paths)
        self.assertIn("/har/save", paths)
        self.assertIn("/tpl/{tplid}/save", paths)


if __name__ == "__main__":
    unittest.main()
