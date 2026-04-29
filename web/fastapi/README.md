# FastAPI Migration — QD Framework

This directory contains the **FastAPI port** for the QD HTTP task scheduler.
As of this version, **FastAPI is the default web framework** when running
`python run.py`.

---

## Default behaviour

| Command | Framework started |
|---------|------------------|
| `python run.py` | FastAPI (uvicorn) — default |
| `WEB_FRAMEWORK=fastapi python run.py` | FastAPI (uvicorn) |
| `WEB_FRAMEWORK=tornado python run.py` | Tornado (legacy) |
| `python run_fastapi.py` | FastAPI (uvicorn) — standalone alias |

The `WEB_FRAMEWORK` environment variable controls which server starts.
The Tornado code is **fully preserved** and can be selected at any time.

---

## Switching back to Tornado

```bash
# One-off
WEB_FRAMEWORK=tornado python run.py

# Persistent (shell)
export WEB_FRAMEWORK=tornado
python run.py

# Docker Compose — add to the service's environment section:
# - WEB_FRAMEWORK=tornado
```

---

## Directory structure

```
web/
├── app.py                  # Tornado Application (unchanged)
├── fastapi_app.py          # FastAPI application factory (create_app)
├── fastapi/
│   ├── __init__.py         # Package marker
│   ├── auth.py             # Tornado-compatible secure-cookie helpers
│   ├── base.py             # Shared FastAPI dependencies
│   ├── templates.py        # Jinja2 render_template helper
│   ├── README.md           # This file
│   └── handlers/           # 18 ported handler modules (Phase 2 complete)
│       ├── __init__.py     # Auto-discovery of APIRouter modules
│       ├── about.py
│       ├── har_ai.py
│       ├── har_editor.py
│       ├─�� index.py
│       ├── login.py
│       ├── my.py
│       ├── push.py
│       ├── site.py
│       ├── subscribe.py
│       ├── task.py
│       ├── task_multi.py
│       ├── task_run.py
│       ├── tpl.py
│       ├── user_mgmt.py
│       ├── user_passwd.py
│       ├── user_register.py
│       ├── util_media.py
│       └── util_simple.py
└── handlers/               # Tornado handlers (unchanged)
```

---

## Relationship with the Tornado application

| Concern            | Tornado (legacy)              | FastAPI (default)                     |
|--------------------|-------------------------------|---------------------------------------|
| Entry point        | `WEB_FRAMEWORK=tornado run.py`| `python run.py` (default)             |
| Standalone script  | N/A                           | `python run_fastapi.py`               |
| Default port       | `8923` (`config.port`)        | `config.port` (same default port)     |
| Custom port        | `-p <port>` or `PORT` env     | `FASTAPI_PORT` env or `-p <port>`     |
| App factory        | `web/app.py:Application`      | `web/fastapi_app.py:create_app`       |
| Handlers           | `web/handlers/*.py`           | `web/fastapi/handlers/*.py`           |
| Worker startup     | `tornado.ioloop.PeriodicCallback` | FastAPI lifespan + `asyncio.create_task` |
| Templates          | `web/tpl/` via Jinja2         | same `web/tpl/` via same Jinja2 env   |
| Static files       | `web/static/` via Tornado     | same `web/static/` via StaticFiles    |
| Cookie auth        | `tornado.web.set_secure_cookie` | `web/fastapi/auth.py` (compatible)  |
| DB / Fetcher       | `application.db` / `.fetcher` | `request.app.state.db` / `.fetcher`   |

---

## Worker startup in FastAPI mode

Workers (`BatchWorker` / `QueueWorker`) are started inside a FastAPI
**lifespan** context manager, which runs before the server accepts requests:

```python
@asynccontextmanager
async def lifespan(app):
    await _start_worker_async(db)   # creates asyncio tasks
    yield
    await engine.dispose()          # clean shutdown
```

- **QueueWorker** — wrapped in `asyncio.create_task(worker())` directly.
- **BatchWorker** — an `asyncio.sleep` loop replaces `tornado.ioloop.PeriodicCallback`,
  keeping the same scheduling interval (`config.check_task_loop` ms).

The `worker.py` file is **not modified** — only the launch mechanism changed.

---

## Running the FastAPI server

```bash
# Default (FastAPI on config.port, usually 8923)
python run.py

# Custom port via env var
FASTAPI_PORT=9000 python run.py

# Standalone alias (identical behaviour)
python run_fastapi.py
FASTAPI_PORT=9000 python run_fastapi.py
```

---

## Ported handlers (Phase 2 complete)

| Handler file                                | Routes (approximate)               | Status |
|---------------------------------------------|------------------------------------|--------|
| `web/fastapi/handlers/about.py`             | `GET /about`                       | Done   |
| `web/fastapi/handlers/index.py`             | `GET /`                            | Done   |
| `web/fastapi/handlers/login.py`             | `/login`, `/logout`                | Done   |
| `web/fastapi/handlers/user_register.py`     | `/register`                        | Done   |
| `web/fastapi/handlers/user_passwd.py`       | `/user/password`                   | Done   |
| `web/fastapi/handlers/user_mgmt.py`         | `/user/*` (admin)                  | Done   |
| `web/fastapi/handlers/my.py`                | `/my`                              | Done   |
| `web/fastapi/handlers/task.py`              | `/task/*`                          | Done   |
| `web/fastapi/handlers/task_multi.py`        | `/task/multi/*`                    | Done   |
| `web/fastapi/handlers/task_run.py`          | `/task/run`                        | Done   |
| `web/fastapi/handlers/tpl.py`               | `/tpl/*`                           | Done   |
| `web/fastapi/handlers/subscribe.py`         | `/subscribe/*`                     | Done   |
| `web/fastapi/handlers/push.py`              | `/push/*`                          | Done   |
| `web/fastapi/handlers/site.py`              | `/site/*`                          | Done   |
| `web/fastapi/handlers/har_editor.py`        | `/har/*`                           | Done   |
| `web/fastapi/handlers/har_ai.py`            | `/har/ai`                          | Done   |
| `web/fastapi/handlers/util_simple.py`       | `/util/*`                          | Done   |
| `web/fastapi/handlers/util_media.py`        | `/util/image`, `/util/ocr`         | Done   |

---

## Known limitations

1. **`static_url` hash versioning** — Tornado's `StaticFileHandler` appends
   `?v=<md5hash>` for cache-busting.  The FastAPI `static_url()` helper in
   `templates.py` currently omits this.  Templates still render correctly;
   only the cache-busting is absent.

2. **`locale` / i18n** — `locale` is injected as `None` into every template.
   QD does not have active i18n, so this is acceptable for now.

3. **`xsrf_token` / `xsrf_form_html`** — XSRF cookies are disabled in the
   Tornado app (`xsrf_cookies=True` is absent from `web/app.py`).  The FastAPI
   equivalents are stubs that return empty strings.

4. **WebSocket handlers** — Not ported.  FastAPI supports WebSockets natively
   via `fastapi.WebSocket`; this will be added per-handler as needed.

5. **`reverse_url`** — A basic name→path lookup is implemented.  It does not
   support all Tornado URL pattern features (e.g. regex groups).
