# FastAPI Migration — QD Framework

This directory contains the **FastAPI port scaffold** for the QD HTTP task
scheduler.  It runs **side-by-side** with the existing Tornado application:
both servers can be active simultaneously on different ports.

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
│   └── handlers/
│       ├── __init__.py     # Auto-discovery of APIRouter modules
│       └── about.py        # POC: port of web/handlers/about.py
└── handlers/               # Tornado handlers (unchanged)
```

---

## Relationship with the Tornado application

| Concern            | Tornado (existing)            | FastAPI (new)                         |
|--------------------|-------------------------------|---------------------------------------|
| Entry point        | `run.py`                      | `run_fastapi.py`                      |
| Default port       | `8923` (`config.port`)        | `8925` (`FASTAPI_PORT` env var)       |
| App factory        | `web/app.py:Application`      | `web/fastapi_app.py:create_app`       |
| Handlers           | `web/handlers/*.py`           | `web/fastapi/handlers/*.py`           |
| Templates          | `web/tpl/` via Jinja2         | same `web/tpl/` via same Jinja2 env   |
| Static files       | `web/static/` via Tornado     | same `web/static/` via StaticFiles    |
| Cookie auth        | `tornado.web.set_secure_cookie` | `web/fastapi/auth.py` (compatible)  |
| DB / Fetcher       | `application.db` / `.fetcher` | `request.app.state.db` / `.fetcher`   |

The Tornado application is **not modified** in any way.  Running
`python run.py` starts Tornado exactly as before.

---

## Running the FastAPI server

```bash
# Install new dependencies (if not already done via pipenv)
pip install fastapi "uvicorn[standard]"

# Start FastAPI on port 8925 (Tornado can run on 8923 simultaneously)
python run_fastapi.py

# Custom port
FASTAPI_PORT=9000 python run_fastapi.py
```

---

## Ported handlers

| Handler file                          | Route         | Status   |
|---------------------------------------|---------------|----------|
| `web/fastapi/handlers/about.py`       | `GET /about`  | Done     |

---

## Pending handlers (to be ported by subsequent agents)

All other handlers in `web/handlers/` are **not yet ported**.  The table
below lists them as a reference for future work:

| Tornado handler file         | Routes (approximate)                    |
|------------------------------|-----------------------------------------|
| `web/handlers/user.py`       | `/login`, `/logout`, `/register`, `/me` |
| `web/handlers/task.py`       | `/task/*`                               |
| `web/handlers/tpl.py`        | `/tpl/*`                                |
| `web/handlers/pubtpl.py`     | `/pubtpl/*`                             |
| `web/handlers/util.py`       | `/util/*`                               |
| `web/handlers/notepad.py`    | `/notepad/*`                            |
| `web/handlers/har.py`        | `/har/*`                                |
| `web/handlers/site.py`       | `/site/*`                               |
| `web/handlers/push.py`       | `/push/*`                               |

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
