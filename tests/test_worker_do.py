# -*- coding: utf-8 -*-
"""Behavioural tests for worker.BaseWorker.do() and its backoff helper.

These cover the signin-reliability fixes:
  [B]   post-processing (cal_next_ts / write success log / task.mod / clear_log)
        failures must NOT flip an already-successful signin into a failure.
  cal_next_ts missing 'ts'  -> degrade to interval, never raise.
  [#23] data-fetch / json parse errors must not escape do(); next must advance.
  [#24] failure backoff must have a >=60s floor; temporary (network/5xx) errors
        must not exhaust the retry budget and permanently disable a task.
  [#36] success/failure stats + push run after commit, each guarded, and never
        affect the success/failure verdict.

A lightweight in-memory fake DB is used so do() can run without the real
SQLAlchemy / crypto stack.
"""
import contextlib
import json
import time

import pytest

import worker


# --------------------------------------------------------------------------- #
# Fake DB / fetcher harness
# --------------------------------------------------------------------------- #
class _FakeUser:
    def __init__(self, db):
        self.db = db

    async def get(self, userid, fields=None, sql_session=None):
        if self.db.user_get_error:
            raise self.db.user_get_error
        return self.db.user_row

    async def decrypt(self, id, data, sql_session=None):
        return data

    async def encrypt(self, id, data, sql_session=None):
        if self.db.encrypt_error:
            raise self.db.encrypt_error
        return data


class _FakeTpl:
    def __init__(self, db):
        self.db = db

    async def get(self, tplid, fields=None, sql_session=None):
        if self.db.tpl_get_error:
            raise self.db.tpl_get_error
        return self.db.tpl_row

    async def incr_success(self, id, sql_session=None):
        if self.db.incr_success_error:
            raise self.db.incr_success_error
        self.db.incr_success_calls.append(id)

    async def incr_failed(self, id, sql_session=None):
        if self.db.incr_failed_error:
            raise self.db.incr_failed_error
        self.db.incr_failed_calls.append(id)


class _FakeTask:
    def __init__(self, db):
        self.db = db

    async def mod(self, taskid, sql_session=None, **fields):
        self.db.task_mod_calls.append(fields)


class _FakeTaskLog:
    def __init__(self, db):
        self.db = db

    async def add(self, taskid, success=None, msg=None, sql_session=None):
        self.db.tasklog_add_calls.append({"success": success, "msg": msg})

    async def list(self, taskid=None, fields=None, sql_session=None, limit=None):
        if self.db.clear_log_should_fail:
            raise RuntimeError("clear_log list boom")
        return []

    async def delete(self, ids, sql_session=None):
        return None


class _FakeSite:
    def __init__(self, db):
        self.db = db

    async def get(self, id, fields=None, sql_session=None):
        return {"logDay": 30}


class FakeDB:
    def __init__(self):
        self.user_row = {
            "id": 10,
            "email": "a@b.c",
            "email_verified": 1,
            "nickname": "nick",
            "logtime": json.dumps({"ErrTolerateCnt": 0}),
        }
        self.tpl_row = {
            "id": 20,
            "userid": 0,
            "sitename": "site",
            "siteurl": "http://x",
            "tpl": {},
            "interval": 3600,
            "last_success": 0,
        }
        # error injection switches
        self.user_get_error = None
        self.tpl_get_error = None
        self.encrypt_error = None
        self.incr_success_error = None
        self.incr_failed_error = None
        self.clear_log_should_fail = False

        # recordings
        self.tasklog_add_calls = []
        self.task_mod_calls = []
        self.incr_success_calls = []
        self.incr_failed_calls = []

        self.user = _FakeUser(self)
        self.tpl = _FakeTpl(self)
        self.task = _FakeTask(self)
        self.tasklog = _FakeTaskLog(self)
        self.site = _FakeSite(self)

    @contextlib.asynccontextmanager
    async def transaction(self, sql_session=None):
        yield object()


class _FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result = result or {"variables": {"__log__": "ok"}, "session": []}
        self.error = error

    async def do_fetch(self, tpl, env, proxies=None):
        if self.error:
            raise self.error
        return self.result, None


class _FakePusher:
    calls = []

    def __init__(self, db, sql_session=None):
        pass

    async def pusher(self, userid, pushsw, kind, title, content):
        _FakePusher.calls.append((userid, kind, title))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_task(**over):
    t = dict(
        id=1,
        note="note",
        userid=10,
        tplid=20,
        disabled=0,
        pushsw=json.dumps({"pushen": True, "sw": True}),
        init_env={},
        newontime=json.dumps({"sw": False}),
        success_count=0,
        failed_count=0,
        last_failed_count=0,
        retry_count=8,
        retry_interval=0,
    )
    t.update(over)
    return t


@pytest.fixture
def make_worker(monkeypatch):
    monkeypatch.setattr(worker, "Pusher", _FakePusher)
    _FakePusher.calls = []

    def _factory(db, fetcher=None):
        w = worker.BaseWorker(db)
        w.fetcher = fetcher or _FakeFetcher()
        return w

    return _factory


def _success_logs(db):
    return [c for c in db.tasklog_add_calls if c["success"] is True]


def _failure_logs(db):
    return [c for c in db.tasklog_add_calls if c["success"] is False]


# --------------------------------------------------------------------------- #
# Happy path baseline
# --------------------------------------------------------------------------- #
async def test_success_baseline(make_worker):
    db = FakeDB()
    w = make_worker(db)
    result = await w.do(make_task())
    assert result is True
    assert len(_success_logs(db)) == 1
    assert _failure_logs(db) == []
    # task.mod success call resets the consecutive failure counter
    assert any(c.get("last_failed_count") == 0 for c in db.task_mod_calls)
    # next advanced into the future
    success_mod = [c for c in db.task_mod_calls if "last_success" in c][0]
    assert success_mod["next"] > time.time()
    assert db.incr_success_calls == [20]


# --------------------------------------------------------------------------- #
# [B] post-processing failures must not flip success -> failure
# --------------------------------------------------------------------------- #
async def test_clear_log_failure_keeps_success(make_worker):
    db = FakeDB()
    db.clear_log_should_fail = True
    w = make_worker(db)
    result = await w.do(make_task())
    assert result is True, "clear_log failure must not turn a success into failure"
    assert len(_success_logs(db)) == 1
    assert _failure_logs(db) == [], "no failure log should be written"


async def test_post_success_encrypt_failure_keeps_success(make_worker):
    db = FakeDB()
    db.encrypt_error = RuntimeError("encrypt boom")
    w = make_worker(db)
    result = await w.do(make_task())
    # signin itself succeeded; bookkeeping write failed -> still success, no
    # failure record / no backoff disable.
    assert result is True
    assert _failure_logs(db) == []


async def test_cal_next_ts_missing_ts_degrades_to_interval(make_worker):
    db = FakeDB()
    w = make_worker(db)
    # ontime/cron with a broken cron value -> cal_next_ts returns no 'ts'
    task = make_task(newontime=json.dumps({"sw": True, "mode": "cron", "cron_val": "not-a-cron"}))
    result = await w.do(task)
    assert result is True, "missing 'ts' must degrade to interval, not raise/fail"
    assert len(_success_logs(db)) == 1
    assert _failure_logs(db) == []
    success_mod = [c for c in db.task_mod_calls if "last_success" in c][0]
    assert success_mod["next"] > time.time()


# --------------------------------------------------------------------------- #
# [#23] data-fetch / parse errors must not escape do(); next must advance
# --------------------------------------------------------------------------- #
async def test_invalid_pushsw_json_does_not_escape(make_worker):
    db = FakeDB()
    w = make_worker(db)
    task = make_task(pushsw="this-is-not-json")
    # must not raise
    result = await w.do(task)
    assert result is True
    assert len(_success_logs(db)) == 1


async def test_user_get_db_error_advances_next(make_worker):
    db = FakeDB()
    db.user_get_error = RuntimeError("db is down")
    w = make_worker(db)
    result = await w.do(make_task())
    assert result is False
    # a failure log is written and next is advanced (not None) so the producer
    # will not tight-loop every ~500ms.
    assert len(_failure_logs(db)) == 1
    failure_mod = [c for c in db.task_mod_calls if "last_failed" in c]
    assert failure_mod, "task.mod must run so next advances"
    assert failure_mod[-1]["next"] is not None
    assert failure_mod[-1]["next"] > time.time()


async def test_tpl_get_db_error_advances_next(make_worker):
    db = FakeDB()
    db.tpl_get_error = RuntimeError("db is down")
    w = make_worker(db)
    result = await w.do(make_task())
    assert result is False
    assert len(_failure_logs(db)) == 1
    failure_mod = [c for c in db.task_mod_calls if "last_failed" in c]
    assert failure_mod and failure_mod[-1]["next"] is not None


# --------------------------------------------------------------------------- #
# Ordinary fetch failure still records failure + backoff
# --------------------------------------------------------------------------- #
async def test_fetch_failure_records_backoff(make_worker):
    db = FakeDB()
    w = make_worker(db, fetcher=_FakeFetcher(error=ValueError("login failed")))
    result = await w.do(make_task())
    assert result is False
    assert len(_failure_logs(db)) == 1
    assert _success_logs(db) == []
    failure_mod = [c for c in db.task_mod_calls if "last_failed" in c][-1]
    assert failure_mod["next"] > time.time()
    assert db.incr_failed_calls == [20]


# --------------------------------------------------------------------------- #
# [#36] stats failure must not flip the verdict / escape do()
# --------------------------------------------------------------------------- #
async def test_incr_success_failure_does_not_flip(make_worker):
    db = FakeDB()
    db.incr_success_error = RuntimeError("stats boom")
    w = make_worker(db)
    result = await w.do(make_task())  # must not raise
    assert result is True
    assert len(_success_logs(db)) == 1


# --------------------------------------------------------------------------- #
# [#24] backoff floor + temporary error handling (pure static method)
# --------------------------------------------------------------------------- #
def test_backoff_floor_60s():
    # interval=5s would otherwise collapse the backoff to 5s
    val = worker.BaseWorker.failed_count_to_time(0, retry_count=8, interval=5)
    assert val == 60


def test_backoff_floor_not_applied_to_explicit_retry_interval():
    # explicit retry_interval is the user's choice and is respected as-is
    val = worker.BaseWorker.failed_count_to_time(0, retry_count=8, retry_interval=5)
    assert val == 5


def test_existing_interval_cap_still_works():
    # regression: 120s interval still caps a 600s backoff to 120s
    val = worker.BaseWorker.failed_count_to_time(0, retry_count=8, interval=120)
    assert val == 120


def test_temporary_error_does_not_exhaust_retries():
    # at the retry ceiling a permanent error disables (None) but a temporary
    # one keeps retrying.
    assert worker.BaseWorker.failed_count_to_time(8, retry_count=8) is None
    assert worker.BaseWorker.failed_count_to_time(8, retry_count=8, is_temporary=True) is not None


def test_temporary_error_respects_explicit_no_retry():
    # retry_count == 0 means the user explicitly disabled retries
    assert worker.BaseWorker.failed_count_to_time(0, retry_count=0, is_temporary=True) is None


def test_is_temporary_error_classification():
    assert worker.BaseWorker._is_temporary_error(TimeoutError("x")) is True
    assert worker.BaseWorker._is_temporary_error(ConnectionError("x")) is True
    assert worker.BaseWorker._is_temporary_error(Exception("Connection timed out")) is True
    assert worker.BaseWorker._is_temporary_error(Exception("HTTP 503 Service Unavailable")) is True
    assert worker.BaseWorker._is_temporary_error(Exception("签到失败: 验证码错误")) is False
    assert worker.BaseWorker._is_temporary_error(ValueError("bad password")) is False
