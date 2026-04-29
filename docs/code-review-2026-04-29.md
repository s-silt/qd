# Code Review — 2026-04-29

**Scope:** Post-merge review of AI intelligent sign-in (ai_client.py), URL auto-capture
(services/playwright/ Python sidecar + services/playwright-go/ Go sidecar), worker N+1
fix (worker.py), SQLAlchemy 2.0 upgrade (db/*.py), and libs/utils.py refactor.

**Reviewer:** Claude (claude-sonnet-4-6)  
**Branch reviewed:** HEAD of `main` at commit `7e22112`  
**Review branch:** `claude/post-merge-review`

---

## Legend

| Symbol | Severity |
|--------|----------|
| 🔴 | Bug / correctness issue — should be fixed before next release |
| 🟡 | Quality / performance concern — fix in upcoming sprint |
| 🟢 | Observation / low-priority suggestion |

---

## 1. Bugs / Correctness Issues (🔴)

### 1.1 Stale `sql_session` passed to `Pusher` after transaction closes  
**File:** `worker.py` line 387  
**Status:** ✅ Fixed in this PR (commit `5cf96ca`)

```python
# BEFORE (bug)
async with self.db.transaction() as sql_session:   # closes here at line 383
    ...
pushtool = Pusher(self.db, sql_session=sql_session) # sql_session is closed!

# AFTER (fix)
pushtool = Pusher(self.db, sql_session=None)  # opens fresh session
```

The second `async with self.db.transaction()` block in `do()` (tpl stats update,
lines 378–383) exits before line 387. Passing the closed `sql_session` to `Pusher`
would cause a `sqlalchemy.exc.InvalidRequestError` ("Session is closed") when the
pusher tries to fetch user notice settings. Passing `None` causes `Pusher` to open
a fresh scoped session, which is the correct behaviour.

---

### 1.2 `judge_res` double-consumes aiohttp response body  
**File:** `libs/funcs.py` lines 29–45  
**Status:** ✅ Fixed in this PR (commit `5cf96ca`)

```python
# BEFORE (bug)
text = await res.text()      # body consumed here
_json = await res.json()     # raises aiohttp.ClientConnectionError or returns empty

# AFTER (fix)
text = await res.text()
_json = json.loads(text)     # parse already-read string
```

`aiohttp` response bodies are streaming; calling `.text()` then `.json()` on the
same `ClientResponse` raises `aiohttp.ClientConnectionError: Response payload is not
completed` or silently returns `{}`. The fix reads the body once and parses JSON
from the string.

---

### 1.3 Possible `userid` accessed before `user` null-check  
**File:** `worker.py` line 250–255  

```python
user = await self.db.user.get(task['userid'], ...)
userid = user['id']          # line 251 — KeyError if user is None dict
if not user:                 # line 252 — too late
    ...
```

`db.user.get()` returns `None` when the user doesn't exist. Accessing `user['id']`
on line 251 would raise `TypeError: 'NoneType' object is not subscriptable` rather
than the graceful "no such user, disabled" path.  
**Not fixed in this PR** (touches core logic; recommend a dedicated fix):

```python
user = await self.db.user.get(task['userid'], ...)
if not user:
    await self.db.tasklog.add(...)
    ...
    return False
userid = user['id']   # safe now
```

---

## 2. Typos Corrected (🟡→fixed)

### 2.1 `NOT_RETYR_CODE` → `NOT_RETRY_CODE`  
**File:** `libs/fetcher.py` line 45  
**Status:** ✅ Fixed in this PR (commit `5cf96ca`)

A misspelling that makes grep/refactoring harder and would break any external
code referencing the module-level name.

### 2.2 `'Unkown'` → `'Unknown'`  
**File:** `worker.py` line 172  
**Status:** ✅ Fixed in this PR (commit `5cf96ca`)

---

## 3. Performance Concerns (🟡)

### 3.1 `asyncio.sleep()` coroutine created but not awaited in runner / producer loops  
**File:** `worker.py` lines 410, 423, 445  

```python
# Inside runner() and producer() while-loops:
sleep = asyncio.sleep(config.check_task_loop / 1000.0)  # creates coroutine object
task = await self.queue.get()                            # may block for a long time
...
await sleep   # only awaited AFTER task processing
```

The intent is to rate-limit the loop, but `asyncio.sleep()` starts timing from
when the coroutine is *created*, not when it is first awaited. If `do(task)` takes
longer than the desired interval the sleep has already expired and `await sleep`
returns immediately — giving no rate-limiting benefit. This isn't a correctness
issue (the loop still works) but the behaviour is misleading; the sleep should be
at the *end* of the loop body or be restructured.

**Recommended pattern:**
```python
while True:
    task = await self.queue.get()
    await self.do(task)
    self.queue.task_done()
    await asyncio.sleep(config.check_task_loop / 1000.0)
```

**Not fixed in this PR** (behaviour change; needs QA).

---

### 3.2 `Pusher.send2dingding` / `send2wxpusher` call `judge_res` then `res.json()` again  
**File:** `libs/funcs.py` lines 221–222, 250–251  

```python
r = await self.judge_res(res)   # consumes body on error; returns "True" on 200
_json = await res.json()        # re-reads already-consumed body on success path
```

When `res.status == 200`, `judge_res` returns `"True"` **without** reading the body,
so the subsequent `await res.json()` is safe on the 200 path. However, this pattern
is fragile — if `judge_res` ever changes to read the body on 200 it would break
silently. The cleaner approach is to read the body once before calling `judge_res`,
or to restructure these callers to not call `judge_res` when they need the JSON body.

**Not fixed in this PR** (refactor, not a current live bug on the 200 path).

---

### 3.3 N+1 in `BatchWorker.run()` – `push_batch` still fires per-user  
**File:** `worker.py` lines 490–520

`BatchWorker.run()` correctly defers `push_batch()` to a single call at the end,
so the N+1 for batch-log delivery is already fixed. However, in `BatchWorker.run()`
each task's `do()` call opens two separate database transactions (main work + tpl
stat update). This is by design but worth noting for future pool-sizing: under a
large task set the connection pool could be saturated.

---

## 4. Code Quality (🟡 / 🟢)

### 4.1 Missing type annotations on `do()`, `runner()`, `producer()`  
**File:** `worker.py`  

`BaseWorker.do(task)` accepts an untyped `dict` and returns `bool`. Both public and
internal callers (runner, BatchWorker) would benefit from:

```python
async def do(self, task: dict) -> bool:
```

`failed_count_to_time` now has full annotations (fixed in this PR).

---

### 4.2 Unused `traceback` import in `worker.py`  
**File:** `worker.py` line 11  

`import traceback` is present but only `config.traceback_print` is used as a flag
passed to the logger. `traceback.print_exc()` appears in `fetcher.py` and `funcs.py`
but not in `worker.py`. This import can be removed.

**Note:** Removing it would be safe but affects `BatchWorker` which uses
`logger_worker.exception(e)` — which internally calls `traceback`. Left as-is to
avoid unintended changes.

---

### 4.3 `_insert_or_update` silently discards return value  
**File:** `db/basedb.py` line 122–125  

```python
async def _insert_or_update(self, insert_stmt: Insert, sql_session=None, **kwargs) -> int:
    async with self.transaction(sql_session) as sql_session:
        insert_stmt.on_duplicate_key_update(**kwargs)   # return value discarded!
        result: Result = await sql_session.execute(insert_stmt)
```

`insert_stmt.on_duplicate_key_update(**kwargs)` returns a **new** statement with
the ON DUPLICATE KEY clause; it does **not** mutate `insert_stmt` in place. The
returned statement is discarded and the original statement (without the upsert
clause) is executed instead. This means all callers that rely on upsert behaviour
silently perform plain INSERTs.

**Recommended fix:**
```python
stmt = insert_stmt.on_duplicate_key_update(**kwargs)
result = await sql_session.execute(stmt)
```

**Not fixed in this PR** — the callers need to be audited to determine whether any
rely on this behaviour accidentally working correctly (e.g. always INSERT on fresh
rows, never needing the update path).

---

### 4.4 `safe_eval.py` — `SAFE_OPCODES` missing Python 3.12+ opcodes  
**File:** `libs/safe_eval.py` lines 102–142  

Python 3.12 removed several opcodes (e.g. `PRECALL`, `PUSH_NULL`) and replaced
them with `CALL_INTRINSIC_1`, `LOAD_SUPER_ATTR`, `COPY_FREE_VARS`, and others.
Running on Python 3.12+ will cause `ValueError: forbidden opcode(s) ...` for
code that would be valid under the whitelist's intent.

The existing `to_opcodes()` helper (line 44) gracefully ignores unknown opcode
names, so old entries don't cause failures — but new opcodes won't be permitted.

**Not fixed in this PR** (needs Python-version matrix testing and careful opcode
additions).

---

### 4.5 Login rate-limiting via Redis `evil` system is present  
**File:** `web/handlers/base.py` line 57, 114–146  

The `evil` rate-limiting system is implemented and active. Login attempts that
fail increment the evil score; reaching `config.evil` bans the IP/user until
the next hour window. This is confirmed working — no gap here.

---

## 5. Security Observations (🟢)

### 5.1 Default secret keys warn but do not block startup  
**File:** `config.py` lines 56–62  

`cookie_secret` and `aes_key` both default to `"binux"`. This has been noted in
prior reviews; a startup warning is emitted but production deployments that forget
to set these keys will silently use weak secrets.

**Suggestion (not implemented):** Emit a `logger.critical` and optionally exit if
the default value is detected in a non-debug, non-test environment.

---

### 5.2 SSRF via `_proxy` env variable not fully mitigated  
**File:** `libs/fetcher.py` lines 985–1008  

A user-controlled `_proxy` env variable is parsed by `parse_url()` and used
directly as the HTTP proxy for subsequent requests. This allows any authenticated
user to route task HTTP requests through arbitrary hosts. This is arguably
by-design (QD is a personal automation platform) but is worth documenting as
intended behaviour.

---

### 5.3 `services/playwright/app.py` ALLOW_HOSTS warning in production  
**File:** `services/playwright/app.py` line 134  

The startup SSRF warning when `ALLOW_HOSTS` is unset was already added by a prior
review. Confirmed present and correct.

---

## 6. `go vet` Results (🟢)

```
cd services/playwright-go && go vet ./...
# (no output — clean)
```

No issues reported. Go tests also pass (`go test ./...`).

---

## 7. Test Coverage Summary

| Area | Before this PR | After this PR |
|------|---------------|---------------|
| `worker.py` pure functions | 0 tests | **23 tests** (failed_count_to_time + fix_next_time) |
| `libs/funcs.py` Cal scheduling | 0 tests | **9 tests** (6 ontime + 3 cron-skip) |
| `services/playwright/button_finder.py` | 0 tests | **10 tests** (score_candidate + pick_button) |
| `libs/ai_client.py` | 27 tests (existing) | 27 tests (unchanged) |
| `services/playwright-go/` | 35 tests (existing) | 35 tests (unchanged) |

---

## 8. Changes Applied in this PR

| Commit | Description |
|--------|-------------|
| `5cf96ca` | **fix:** Stale sql_session in `do()`, NOT_RETYR_CODE typo, double-body-read in `judge_res`, 'Unkown' typo |
| `7a33f3f` | **chore:** Add type hints + docstring to `BaseWorker.failed_count_to_time` |
| `680b66e` | **test:** 42 new unit tests for worker/funcs/button_finder pure functions |

---

## 9. Top 3 Items to Follow Up (not fixed here)

### P1 — `_insert_or_update` discards ON DUPLICATE KEY clause (§4.3)
`insert_stmt.on_duplicate_key_update()` returns a new statement; the current code
ignores the return value and executes the original INSERT. All callers relying on
upsert semantics silently perform plain INSERTs instead. **Audit `db/` callers and
apply the one-line fix.**

### P2 — `userid` accessed before `user is None` guard in `do()` (§1.3)
`worker.py:251` accesses `user['id']` before the `if not user:` check on line 252.
A non-existent task owner will raise `TypeError` instead of the intended graceful
disable path. **Swap lines 251–255** to move the null-guard before the attribute
access.

### P3 — `asyncio.sleep` created before blocking work in runner/producer (§3.1)
The pre-created sleep coroutine in `QueueWorker.runner()` and `producer()` does not
actually rate-limit the loop when tasks are slow. This is benign in normal operation
but the pattern is misleading and will bite anyone who tries to tune `CHECK_TASK_LOOP`
for back-pressure. **Move `await asyncio.sleep(...)` to the end of each loop body.**
