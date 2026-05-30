# QD Playwright-Go Sidecar（已废弃 / DEPRECATED）

> ⚠️ **已废弃,无需部署。** `URL 自动抓包` 现已集成进 QD 主镜像（进程内置 Playwright），不再需要任何 sidecar，本目录仅作历史保留。

Lightweight Go replacement for `services/playwright` (Python/Playwright).
Both services coexist; this one is the opt-in alternative when image size and
startup time matter.

| | Python sidecar | Go sidecar |
|---|---|---|
| Base image | `mcr.microsoft.com/playwright/python:v1.49.0` | `chromedp/headless-shell:131.0.6778.86` |
| Estimated image size | ~1.5 GB | ~200–250 MB |
| Cold-start | 3–8 s | <1 s |
| Runtime | CPython + uvicorn | statically-linked Go binary |
| HAR recording | Playwright native (file) | CDP network events (in-memory) |
| Port | 8924 | 8924 (same) |

## HTTP API — identical schema to Python version

### `POST /capture`

**Request** (`Content-Type: application/json`):

```json
{
  "url":               "https://example.com/sign",
  "storage_state":     { "cookies": [...], "origins": [...] },
  "cookies":           "session=abc; token=xyz",
  "hint":              "每日签到",
  "selector":          "[data-testid='signin-btn']",
  "user_agent":        "Mozilla/5.0 ...",
  "viewport":          { "width": 1280, "height": 800 },
  "locale":            "zh-CN",
  "timezone_id":       "Asia/Shanghai",
  "timeout_ms":        60000,
  "wait_after_click_ms": 3000
}
```

All fields except `url` are optional. If both `storage_state` and `cookies` are
provided, `storage_state` takes precedence.

**Response**:

```json
{
  "ok":           true,
  "har":          { "log": { "version": "1.2", "creator": {...}, "entries": [...] } },
  "actions":      [{"type": "navigate", "url": "..."}, {"type": "click", ...}],
  "found_button": { "text": "立即签到", "selector": "[data-testid='sign']", "quality": "stable" },
  "candidates":   [ ... up to 10 scored button candidates ... ],
  "error":        null,
  "elapsed_ms":   1234
}
```

Field names are **identical** to the Python version — QD main-end code (`web/handlers/har.py`)
requires no changes to switch between sidecars.

### `GET /health`

```json
{ "ok": true, "browser_ready": true, "max_concurrent": 2 }
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `HEADLESS` | `true` | Set `false` for debugging |
| `MAX_CONCURRENT` | `2` | Max simultaneous browser sessions |
| `DEFAULT_TIMEOUT_MS` | `60000` | Per-capture timeout (ms) |
| `ALLOW_HOSTS` | _(empty — any host)_ | Comma-separated host whitelist (e.g. `example.com,foo.com`). Empty = allow any host (SSRF risk in production). |
| `PORT` | `8924` | Listen port |

## Running with Docker Compose

Add the following service to your `docker-compose.yml` (or a local override file):

```yaml
services:
  playwright-go:
    build:
      context: ./services/playwright-go
      dockerfile: Dockerfile
    ports:
      - "8924:8924"
    environment:
      - HEADLESS=true
      - MAX_CONCURRENT=2
      - ALLOW_HOSTS=example.com,yoursite.com
    restart: unless-stopped
    shm_size: 256m        # prevent Chrome OOM on shared memory
```

Then point QD's `PLAYWRIGHT_URL` to `http://playwright-go:8924` instead of
`http://playwright:8924`.

## Building the Docker image

```bash
# From repo root:
docker build -f services/playwright-go/Dockerfile services/playwright-go/ -t qd-playwright-go
```

## Running unit tests

```bash
cd services/playwright-go
go test ./... -v
```

All unit tests are pure Go (no browser required):

- `security_test.go` — 12 tests: cookie parsing, domain matching, cross-domain drop,
  substring-attack defense
- `button_finder_test.go` — 19 tests: score weights, hint override, JS script
  static checks

```
ok  github.com/silt/qd/services/playwright-go  0.006s
```

## Differences from Python version

### Functional differences (behaviour-compatible)

- **HAR recording**: Python uses Playwright's native `record_har_path` file;
  Go uses CDP `Network.enable` + event listeners to build HAR in-memory.
  The resulting structure is HAR 1.2 compliant and identical to what QD main-end
  expects (`har.log.entries[].request/response`).
- **Response body**: fetched via `Network.getResponseBody` CDP call per finished
  request. Very large bodies (> a few MB) may be omitted if Chrome has already
  evicted them from the cache.
- **wait_after_click network-idle**: Python uses Playwright's `waitForLoadState("networkidle")`;
  Go uses `chromedp.WaitReady("body")` which is "DOM ready" not strictly network-idle.
  In practice the difference is negligible for sign-in flows.
- **Locale / timezone injection**: Playwright sets locale/timezone at context creation;
  chromedp sets viewport via `Emulation.setDeviceMetricsOverride` but locale/timezone
  are not directly controllable via CDP in the same way. The Go version respects
  `viewport` and `user_agent`; locale/timezone fields are accepted but currently
  have no effect on the headless-shell binary.

### Known limitations

1. **`sub.example.com` cookie not matching parent request**: Python Playwright
   follows RFC 6265 exactly. The Go security layer (`DomainMatches`) also follows
   RFC 6265 — `sub.example.com` domain cookie does NOT match `example.com` request
   (only `.example.com` with leading dot matches both). This is intentional and
   consistent with Python behaviour.
2. **No page screenshot / PDF**: out of scope for this sidecar.
3. **No cross-origin iframe HAR**: CDP network events are scoped to the main frame's
   network stack; cross-origin iframe requests are also captured (CDP sees all
   network activity in the tab), but the body may not always be retrievable.
