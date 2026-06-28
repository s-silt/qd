# -*- coding: utf-8 -*-
"""Authorization / CSRF / resource-limit tests for web.handlers.

These cover the audit findings assigned to the *handlers* file group:

  [E #13]   TaskSetTimeHandler.post  -> must check_permission('w') before
            mutating another user's schedule.
  [E #12/14] TaskLogDelHandler.post  -> must check_permission before wiping
            another user's signin logs.
  [E #30/45] TasksDelHandler setGroup -> must check_permission AND write the
            correct column (_groups, not the non-existent `groups`).
  [E #11]   UserManagerHandler.get   -> admin gate must use self.current_user,
            NOT the URL `userid` (otherwise any user hits /user/<adminid>/manage
            and dumps every email/IP).
  [E #33]   destructive log wipes must be POST (no <img src> GET CSRF) and still
            enforce ownership.
  [P1 #15]  HARAIAnalyze.post -> reject oversized HAR by bytes before json.loads
            and offload CPU-bound preprocess off the IOLoop thread.

The handler coroutines are exercised directly (real check_permission /
permission / evil chain from BaseHandler) against an in-memory fake DB, so no
network / SQLAlchemy / template stack is required.
"""
import json
import threading

import pytest
from tornado.web import HTTPError

import web.handlers.task as task_mod
import web.handlers.user as user_mod
import web.handlers.har as har_mod
from web.handlers.task import (
    TaskSetTimeHandler,
    TaskLogDelHandler,
    TaskLogSuccessDelHandler,
    TaskLogFailDelHandler,
    TasksDelHandler,
)
from web.handlers.user import UserManagerHandler
from web.handlers.har import HARAIAnalyze


# --------------------------------------------------------------------------- #
# In-memory fakes
# --------------------------------------------------------------------------- #
class FakeRedis:
    def evil(self, ip, userid, cnt=None):
        pass

    def is_evil(self, ip, userid=None):
        return False


class _Txn:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


class FakeTaskTable:
    def __init__(self, rows):
        # rows: dict[int, dict]
        self.rows = rows
        self.mod_calls = []
        self.delete_calls = []

    async def get(self, taskid, fields=None, sql_session=None):
        row = self.rows.get(int(taskid))
        if row is None:
            return None
        if fields:
            return {k: row.get(k) for k in fields}
        return dict(row)

    async def mod(self, taskid, sql_session=None, **kw):
        self.mod_calls.append((int(taskid), kw))
        self.rows.setdefault(int(taskid), {}).update(kw)

    async def delete(self, taskid, sql_session=None):
        self.delete_calls.append(int(taskid))
        self.rows.pop(int(taskid), None)

    async def list(self, userid=None, fields=None, limit=None, sql_session=None):
        out = []
        for tid, row in self.rows.items():
            if userid is not None and row.get("userid") != userid:
                continue
            out.append({k: row.get(k) for k in (fields or row.keys())})
        return out


class FakeTaskLogTable:
    def __init__(self, logs):
        # logs: dict[taskid, list[log dict]]
        self.logs = logs
        self.delete_calls = []

    async def list(self, taskid=None, fields=None, sql_session=None):
        rows = self.logs.get(int(taskid), [])
        if fields:
            return [{k: r.get(k) for k in fields} for r in rows]
        return [dict(r) for r in rows]

    async def delete(self, logid, sql_session=None):
        self.delete_calls.append(logid)
        for rows in self.logs.values():
            rows[:] = [r for r in rows if r.get("id") != logid]


class FakeUserTable:
    def __init__(self, rows):
        self.rows = rows
        self.list_calls = 0

    async def get(self, userid, fields=None, sql_session=None):
        row = self.rows.get(int(userid))
        if row is None:
            return None
        if fields:
            return {k: row.get(k) for k in fields}
        return dict(row)

    async def list(self, fields=None, limit=None, sql_session=None):
        self.list_calls += 1
        out = []
        for row in self.rows.values():
            out.append({k: row.get(k) for k in (fields or row.keys())})
        return out


class FakeDB:
    def __init__(self, tasks=None, tasklogs=None, users=None):
        self.task = FakeTaskTable(tasks or {})
        self.tasklog = FakeTaskLogTable(tasklogs or {})
        self.user = FakeUserTable(users or {})
        self.redis = FakeRedis()

    def transaction(self):
        return _Txn()


class FakeRequest:
    def __init__(self, body=b"", body_arguments=None, arguments=None, remote_ip="1.2.3.4"):
        self.body = body
        self.body_arguments = body_arguments or {}
        self.arguments = arguments or {}
        self.remote_ip = remote_ip
        self.headers = {}


def make_handler(cls, db, current_user, request=None):
    """Instantiate a handler bypassing tornado's HTTP machinery."""
    h = cls.__new__(cls)
    h.application = None
    h.db = db
    h._current_user = current_user
    h.request = request or FakeRequest()
    # Record-only stubs for response side effects.
    h._render_calls = []
    h._finish_calls = []
    h._redirect_calls = []

    async def _render(template, **kw):
        h._render_calls.append((template, kw))

    async def _finish(*a, **kw):
        h._finish_calls.append((a, kw))

    def _redirect(url, *a, **kw):
        h._redirect_calls.append(url)

    h.render = _render
    h.finish = _finish
    h.redirect = _redirect
    return h


OWNER = {"id": 7, "isadmin": False, "role": "user"}
OTHER = {"id": 99, "isadmin": False, "role": "user"}
ADMIN = {"id": 1, "isadmin": True, "role": "admin"}


# --------------------------------------------------------------------------- #
# [E #13] TaskSetTimeHandler.post ownership
# --------------------------------------------------------------------------- #
async def test_settime_post_rejects_foreign_task():
    db = FakeDB(tasks={5: {"id": 5, "userid": OWNER["id"], "newontime": "{}"}})
    h = make_handler(
        TaskSetTimeHandler,
        db,
        OTHER,
        FakeRequest(body_arguments={"sw": [b"false"]}),
    )
    with pytest.raises(HTTPError) as ei:
        await h.post("5")
    assert ei.value.status_code == 401
    assert db.task.mod_calls == [], "must not mutate a task the user does not own"


async def test_settime_post_owner_allowed():
    db = FakeDB(tasks={5: {"id": 5, "userid": OWNER["id"], "newontime": "{}"}})
    h = make_handler(
        TaskSetTimeHandler,
        db,
        OWNER,
        FakeRequest(body_arguments={"sw": [b"false"]}),
    )
    await h.post("5")
    assert any(tid == 5 for tid, _ in db.task.mod_calls)


# --------------------------------------------------------------------------- #
# [E #12/14] TaskLogDelHandler.post ownership
# --------------------------------------------------------------------------- #
async def test_logdel_post_rejects_foreign_task():
    db = FakeDB(
        tasks={5: {"id": 5, "userid": OWNER["id"]}},
        tasklogs={5: [{"id": 1, "success": 1, "ctime": 0, "msg": "x"}]},
    )
    h = make_handler(TaskLogDelHandler, db, OTHER, FakeRequest(body_arguments={}))
    with pytest.raises(HTTPError) as ei:
        await h.post("5")
    assert ei.value.status_code == 401
    assert db.tasklog.delete_calls == [], "must not delete another user's logs"


async def test_logdel_post_owner_clear_all():
    db = FakeDB(
        tasks={5: {"id": 5, "userid": OWNER["id"]}},
        tasklogs={5: [{"id": 1, "success": 1, "ctime": 0, "msg": "x"},
                      {"id": 2, "success": 0, "ctime": 0, "msg": "y"}]},
    )
    h = make_handler(TaskLogDelHandler, db, OWNER, FakeRequest(body_arguments={}))
    await h.post("5")
    # no `day` -> clear all + reset counters
    assert set(db.tasklog.delete_calls) == {1, 2}
    reset = [kw for tid, kw in db.task.mod_calls if tid == 5]
    assert reset and reset[0].get("success_count") == 0 and reset[0].get("failed_count") == 0


async def test_logdel_get_is_not_destructive():
    """The old destructive GET (img-src CSRF vector) must be gone."""
    db = FakeDB(
        tasks={5: {"id": 5, "userid": OWNER["id"]}},
        tasklogs={5: [{"id": 1, "success": 1, "ctime": 0, "msg": "x"}]},
    )
    h = make_handler(TaskLogDelHandler, db, OWNER, FakeRequest(body_arguments={}))
    get = getattr(h, "get", None)
    if get is not None:
        # If a GET still exists it must not delete anything.
        try:
            await get("5")
        except HTTPError:
            pass
    assert db.tasklog.delete_calls == [], "GET must never delete logs"


# --------------------------------------------------------------------------- #
# [E #33] Success/Fail log wipes are POST + ownership
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", [TaskLogSuccessDelHandler, TaskLogFailDelHandler])
async def test_success_fail_del_is_post(cls):
    assert "post" in cls.__dict__, f"{cls.__name__} must define a POST handler"
    # destructive GET must not exist (was the <img src> CSRF vector). The default
    # RequestHandler.get (405) is inherited; the class itself must not define one.
    assert "get" not in cls.__dict__, f"{cls.__name__} must not keep a destructive GET"


@pytest.mark.parametrize("cls", [TaskLogSuccessDelHandler, TaskLogFailDelHandler])
async def test_success_fail_del_rejects_foreign_task(cls):
    db = FakeDB(
        tasks={5: {"id": 5, "userid": OWNER["id"]}},
        tasklogs={5: [{"id": 1, "success": 1, "ctime": 0, "msg": "x"},
                      {"id": 2, "success": 0, "ctime": 0, "msg": "y"}]},
    )
    h = make_handler(cls, db, OTHER, FakeRequest(body_arguments={}))
    with pytest.raises(HTTPError) as ei:
        await h.post("5")
    assert ei.value.status_code == 401
    assert db.tasklog.delete_calls == []


# --------------------------------------------------------------------------- #
# [E #30/45] TasksDelHandler setGroup ownership + correct column
# --------------------------------------------------------------------------- #
async def test_tasksdel_setgroup_rejects_foreign_task():
    db = FakeDB(tasks={5: {"id": 5, "userid": OWNER["id"], "_groups": "None"}})
    body = {
        "taskids": [b"[5]"],
        "func": [b"setGroup"],
        "groupValue": [b"hacked"],
    }
    h = make_handler(TasksDelHandler, db, OTHER, FakeRequest(body_arguments=body))
    await h.post(str(OTHER["id"]))
    # permission failure is caught by the handler's try/except (renders an error
    # page) but the mutation must NOT have happened.
    assert db.task.mod_calls == [], "must not regroup another user's task"


async def test_tasksdel_setgroup_owner_uses_groups_column():
    db = FakeDB(tasks={5: {"id": 5, "userid": OWNER["id"], "_groups": "None"}})
    body = {
        "taskids": [b"[5]"],
        "func": [b"setGroup"],
        "groupValue": [b"newgrp"],
    }
    h = make_handler(TasksDelHandler, db, OWNER, FakeRequest(body_arguments=body))
    await h.post(str(OWNER["id"]))
    assert db.task.mod_calls, "owner regroup must mutate the task"
    tid, kw = db.task.mod_calls[0]
    assert tid == 5
    assert "_groups" in kw and kw["_groups"] == "newgrp", "must write _groups column"
    assert "groups" not in kw, "the bogus `groups` column must not be used"


# --------------------------------------------------------------------------- #
# [E #11] UserManagerHandler.get admin gate via current_user
# --------------------------------------------------------------------------- #
async def test_usermanage_get_normal_user_no_admin_leak():
    users = {
        1: {"id": 1, "role": "admin", "email": "admin@x", "status": "Enable",
            "ctime": 0, "atime": 0, "email_verified": 1, "aip": b""},
        7: {"id": 7, "role": "user", "email": "u@x", "status": "Enable",
            "ctime": 0, "atime": 0, "email_verified": 1, "aip": b""},
    }
    db = FakeDB(users=users)
    # OWNER (non-admin) visits /user/1/manage (1 == admin's id)
    h = make_handler(UserManagerHandler, db, OWNER, FakeRequest(arguments={}))
    await h.get("1")
    assert db.user.list_calls == 0, "non-admin must not trigger full user dump"
    assert h._render_calls, "should still render the page"
    _, kw = h._render_calls[0]
    assert kw.get("adminflg") is False
    assert kw.get("users") == []


async def test_usermanage_get_admin_lists_users():
    users = {
        1: {"id": 1, "role": "admin", "email": "admin@x", "status": "Enable",
            "ctime": 0, "atime": 0, "email_verified": 1, "aip": b""},
    }
    db = FakeDB(users=users)
    h = make_handler(UserManagerHandler, db, ADMIN, FakeRequest(arguments={}))
    await h.get("1")
    assert db.user.list_calls == 1
    _, kw = h._render_calls[0]
    assert kw.get("adminflg") is True
    assert kw.get("users")


# --------------------------------------------------------------------------- #
# [P1 #15] HARAIAnalyze byte limit + executor offload
# --------------------------------------------------------------------------- #
class _EnabledClient:
    enabled = True
    model = "test-model"

    def __init__(self, *a, **kw):
        pass


async def test_har_upload_rejected_when_too_large(monkeypatch):
    monkeypatch.setattr(har_mod.ai_client, "AIClient", _EnabledClient)
    called = {"analyze": False}

    async def _spy(*a, **kw):
        called["analyze"] = True
        return {}

    monkeypatch.setattr(har_mod, "_analyze_har_with_ai", _spy)

    limit = har_mod.HAR_UPLOAD_MAX_BYTES
    big = b'{"log": "' + b"a" * (limit + 1) + b'"}'
    db = FakeDB()
    h = make_handler(HARAIAnalyze, db, OWNER, FakeRequest(body=big))
    await h.post()
    assert getattr(h, "_status_code", None) == 413
    assert called["analyze"] is False, "must reject by bytes before any AI work"


async def test_har_upload_small_reaches_analyze(monkeypatch):
    monkeypatch.setattr(har_mod.ai_client, "AIClient", _EnabledClient)
    captured = {}

    async def _spy(har, hint):
        captured["har"] = har
        return {"har": [], "result": {}, "stats": {"input_entries": 0}}

    monkeypatch.setattr(har_mod, "_analyze_har_with_ai", _spy)

    db = FakeDB()
    body = json.dumps({"har": {"log": {"entries": []}}, "hint": ""}).encode()
    h = make_handler(HARAIAnalyze, db, OWNER, FakeRequest(body=body))
    await h.post()
    assert captured.get("har") == {"log": {"entries": []}}


async def test_preprocess_har_runs_off_event_loop_thread(monkeypatch):
    """[#15] CPU-bound preprocess must run in an executor, not block the IOLoop."""
    main_thread = threading.get_ident()
    seen = {}

    def fake_preprocess(har, max_entries):
        seen["thread"] = threading.get_ident()
        return [{"dummy": 1}]

    monkeypatch.setattr(har_mod.ai_client, "preprocess_har", fake_preprocess)
    monkeypatch.setattr(har_mod.ai_client, "build_messages", lambda slim, hint="": [])
    monkeypatch.setattr(har_mod.ai_client, "parse_ai_response", lambda c: {})
    monkeypatch.setattr(har_mod.ai_client, "ai_result_to_har", lambda r: [])

    class _Client:
        enabled = True
        model = "m"

        async def chat(self, messages, temperature=0.1):
            return "{}"

    monkeypatch.setattr(har_mod.ai_client, "AIClient", lambda *a, **k: _Client())

    out = await har_mod._analyze_har_with_ai({"log": {"entries": []}}, "")
    assert "thread" in seen
    assert seen["thread"] != main_thread, "preprocess_har must run off the event-loop thread"
    assert out["stats"]["input_entries"] == 1
