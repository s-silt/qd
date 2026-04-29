#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase 2 smoke tests for FastAPI port of site / tpl / push handlers.

Tests:
  1.  site manage anonymous → 401
  2.  site manage non-admin logged-in → 200 (limited view)
  3.  tpl list /tpls/public anonymous → 200
  4.  tpl var GET anonymous (public tpl) → 200
  5.  tpl var GET anonymous (private tpl) → 403
  6.  tpl del unauthorized (user B cannot delete user A's tpl) → 401
  7.  tpl del own tpl → redirect 303
  8.  push list anonymous → 401
  9.  push list logged-in → 200
 10.  push action unauthorized → 401
 11.  tpl push GET no permission → 403
 12.  tpl group GET anonymous → 401

These tests use MagicMock DB/Fetcher objects so they run without a real database.
Skipped automatically when fastapi / httpx are not installed.
"""

import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Conditional skip
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed — skipping FastAPI Phase 2 tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_db():
    """Build a minimal mock DB that satisfies the handler calls."""
    db = MagicMock()

    # site table
    db.site.get = AsyncMock(return_value={
        'regEn': 1, 'MustVerifyEmailEn': 0, 'logDay': 7
    })
    db.site.mod = AsyncMock()

    # user table
    db.user.get = AsyncMock(return_value={
        'id': 1, 'role': 'normal', 'email': 'user@example.com',
        'email_verified': 1, 'nickname': 'User',
    })
    db.user.challenge_md5 = AsyncMock(return_value=False)
    db.user.decrypt = AsyncMock(return_value=[])
    db.user.encrypt = AsyncMock(return_value=b'encrypted')

    # tpl table — used across multiple handlers
    _tpl_a = {
        'id': 10, 'userid': 1, 'sitename': 'Site A', 'siteurl': 'https://a.example',
        'public': 0, 'note': 'note', 'banner': '', 'tpl': b'tpl',
        'variables': '{}', 'init_env': '{}', 'disabled': 0, 'lock': False,
        'last_success': 0, 'ctime': 0, 'mtime': 0, 'fork': None,
        'success_count': 5, '_groups': 'default', 'interval': 86400,
    }
    _tpl_b = {
        'id': 20, 'userid': 2, 'sitename': 'Site B', 'siteurl': 'https://b.example',
        'public': 0, 'note': 'note', 'banner': '', 'tpl': b'tpl',
        'variables': '{}', 'init_env': '{}', 'disabled': 0, 'lock': False,
        'last_success': 0, 'ctime': 0, 'mtime': 0, 'fork': None,
        'success_count': 3, '_groups': 'default', 'interval': 86400,
    }

    async def _tpl_get(tplid, fields=None, sql_session=None):
        tpl = _tpl_a if int(tplid) == 10 else (_tpl_b if int(tplid) == 20 else None)
        if tpl is None:
            return None
        if fields:
            return {k: v for k, v in tpl.items() if k in fields}
        return dict(tpl)

    db.tpl.get = AsyncMock(side_effect=_tpl_get)
    db.tpl.list = AsyncMock(return_value=[_tpl_a, _tpl_b])
    db.tpl.mod = AsyncMock()
    db.tpl.delete = AsyncMock()
    db.tpl.add = AsyncMock(return_value=30)
    db.tpl.incr_success = AsyncMock()

    # push_request table
    db.push_request.PENDING = 0
    db.push_request.ACCEPT = 1
    db.push_request.REFUSE = 2
    db.push_request.CANCEL = 3

    _pr = {
        'id': 100, 'from_tplid': 10, 'from_userid': 1, 'to_tplid': None,
        'to_userid': 2, 'status': 0, 'msg': 'please share',
    }

    async def _pr_get(prid, fields=None, sql_session=None):
        if int(prid) == 100:
            if fields:
                return {k: v for k, v in _pr.items() if k in fields}
            return dict(_pr)
        return None

    db.push_request.get = AsyncMock(side_effect=_pr_get)
    db.push_request.list = AsyncMock(return_value=[])
    db.push_request.add = AsyncMock(return_value=101)
    db.push_request.mod = AsyncMock()

    # transaction context manager
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _tx():
        yield None

    db.transaction = _tx

    # redis (evil counter)
    db.redis = MagicMock()
    db.redis.is_evil = MagicMock(return_value=False)
    db.redis.evil = MagicMock()

    return db


def _make_mock_fetcher():
    fetcher = MagicMock()
    fetcher.tpl2har = MagicMock(return_value={})
    fetcher.do_fetch = AsyncMock(return_value=({'variables': {'__log__': 'ok'}}, None))
    return fetcher


def _make_app(db=None, fetcher=None):
    """Create a FastAPI test app with mock DB and Fetcher."""
    from web.fastapi_app import create_app
    return create_app(db=db or _make_mock_db(), fetcher=fetcher or _make_mock_fetcher(), version="test")


def _make_user_cookie(user_dict: dict) -> str:
    """Produce a signed 'user' cookie value that get_current_user can decode."""
    import umsgpack
    from web.fastapi.auth import create_signed_value
    import config
    raw = umsgpack.packb(user_dict)
    return create_signed_value("user", raw, secret=config.cookie_secret)


_USER_A = {'id': 1, 'role': 'normal', 'email': 'a@example.com', 'nickname': 'UserA', 'isadmin': False}
_USER_B = {'id': 2, 'role': 'normal', 'email': 'b@example.com', 'nickname': 'UserB', 'isadmin': False}
_ADMIN  = {'id': 3, 'role': 'admin',  'email': 'admin@example.com', 'nickname': 'Admin', 'isadmin': True}


# ---------------------------------------------------------------------------
# Test: site handler
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestSiteHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 1: anonymous → 401
    def test_site_manage_anonymous_401(self):
        resp = self.client.get("/site/1/manage")
        self.assertEqual(resp.status_code, 401)

    # Case 2: logged-in non-admin → 200 (limited view without adminflg)
    def test_site_manage_logged_in_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/site/1/manage", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)

    # Case 3: admin GET → 200
    def test_site_manage_admin_get_200(self):
        # Make db.user.get return an admin user
        self.db.user.get = AsyncMock(return_value={
            'id': 3, 'role': 'admin', 'email': 'admin@example.com',
            'email_verified': 1, 'nickname': 'Admin',
        })
        cookie = _make_user_cookie(_ADMIN)
        resp = self.client.get("/site/3/manage", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Test: tpl handler
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestTplHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 4: public tpl list anonymous → 200
    def test_public_tpls_anonymous_200(self):
        resp = self.client.get("/tpls/public")
        self.assertEqual(resp.status_code, 200)

    # Case 5: tpl var GET — user owns tpl → 200
    def test_tpl_var_owner_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/tpl/10/var", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)

    # Case 6: tpl var GET — user B accessing user A's private tpl → 403
    def test_tpl_var_other_user_403(self):
        cookie = _make_user_cookie(_USER_B)
        resp = self.client.get("/tpl/10/var", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 403)

    # Case 7: tpl del — user B cannot delete user A's tpl → 401
    def test_tpl_del_unauthorized_401(self):
        cookie = _make_user_cookie(_USER_B)
        resp = self.client.post("/tpl/10/del", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 401)

    # Case 8: tpl del — owner deletes own tpl → 303 redirect
    def test_tpl_del_owner_303(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post("/tpl/10/del", cookies={"user": cookie}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    # Case 9: tpl push GET without permission → 403 (user B on tpl 10)
    def test_tpl_push_no_permission_403(self):
        cookie = _make_user_cookie(_USER_B)
        resp = self.client.get("/tpl/10/push", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 403)

    # Case 10: tpl push GET with permission → 200 (user A on tpl 10)
    def test_tpl_push_owner_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/tpl/10/push", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)

    # Case 11: tpl group GET anonymous → 401
    def test_tpl_group_anonymous_401(self):
        resp = self.client.get("/tpl/10/group")
        self.assertEqual(resp.status_code, 401)

    # Case 12: tpl group GET logged-in owner → 200
    def test_tpl_group_owner_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/tpl/10/group", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Test: push handler
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestPushHandler(unittest.TestCase):

    def setUp(self):
        self.db = _make_mock_db()
        self.app = _make_app(db=self.db)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # Case 13: push list anonymous → 401
    def test_push_list_anonymous_401(self):
        resp = self.client.get("/pushs")
        self.assertEqual(resp.status_code, 401)

    # Case 14: push list logged-in → 200
    def test_push_list_logged_in_200(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.get("/pushs", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 200)

    # Case 15: push action cancel by wrong user → 401
    def test_push_action_wrong_user_401(self):
        # pr 100 has from_userid=1 (USER_A); USER_B cannot cancel it
        cookie = _make_user_cookie(_USER_B)
        resp = self.client.post("/push/100/cancel", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 401)

    # Case 16: push action cancel by owner → 303 redirect
    def test_push_action_cancel_owner_303(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post("/push/100/cancel", cookies={"user": cookie}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    # Case 17: push view GET anonymous → 401
    def test_push_view_anonymous_401(self):
        resp = self.client.get("/push/100/view")
        self.assertEqual(resp.status_code, 401)

    # Case 18: push action on non-existent prid → 404
    def test_push_action_not_found_404(self):
        cookie = _make_user_cookie(_USER_A)
        resp = self.client.post("/push/9999/cancel", cookies={"user": cookie})
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Test: router auto-discovery includes new routes
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestPhase2RouterDiscovery(unittest.TestCase):

    def test_tpl_routes_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, 'path')]
        self.assertIn("/tpls/public", paths)
        self.assertIn("/tpl/{tplid}/del", paths)
        self.assertIn("/tpl/{tplid}/group", paths)

    def test_push_routes_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, 'path')]
        self.assertIn("/pushs", paths)
        self.assertIn("/push/{prid}/{action}", paths)
        self.assertIn("/push/{prid}/view", paths)

    def test_site_routes_registered(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, 'path')]
        self.assertIn("/site/{userid}/manage", paths)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
