# Playwright sidecar（已废弃 / DEPRECATED）

> ⚠️ **本 sidecar 已废弃,无需部署。** `URL 自动抓包` 现已集成进 QD 主镜像（进程内置 Playwright，主 `Dockerfile` 已内置 playwright + chromium），不再需要独立 sidecar，也不再接入 `docker-compose`。详见 [`auto-capture.md`](../../web/docs/zh_CN/guide/auto-capture.md)。本目录仅作历史保留。

QD `URL 自动抓包` 功能的独立服务容器。给定 URL + Cookie/storage_state, 启动 headless Chromium 加载页面、点击签到按钮、录制 HAR 返回给 QD 主端。

详细使用说明：[../web/docs/zh_CN/guide/auto-capture.md](../../web/docs/zh_CN/guide/auto-capture.md)

## 目录

```
services/playwright/
├── Dockerfile           # 基于 mcr.microsoft.com/playwright/python:v1.49-jammy
├── app.py               # FastAPI POST /capture (生产入口)
├── button_finder.py     # 启发式按钮评分 + JS 候选采集脚本
├── security.py          # storage_state 跨域剔除 / cookie 字符串解析
├── requirements.txt     # 运行时依赖 (FastAPI/uvicorn/pydantic)
├── requirements-dev.txt # 测试依赖 (pytest/pytest-asyncio/aiohttp)
├── pytest.ini           # pytest 配置 (asyncio_mode=auto)
├── conftest.py          # 集成测试 fixture (browser / site server)
├── test_button_finder.py    # 纯函数单元测试 (~20 用例, 无外部依赖)
├── test_integration.py      # 真实浏览器集成测试 (~9 用例, 需 playwright)
└── test_pages/          # 测试用静态 HTML
    ├── sign.html
    └── login.html
```

## 单元测试（无外部依赖）

```bash
# 在仓库根目录
python -m unittest services.playwright.test_button_finder
```

20 个用例覆盖按钮打分、storage_state 剔除、JS 脚本静态守卫等纯逻辑。

## 集成测试（需要 Playwright + Chromium）

集成测试启动真实 Chromium 浏览器访问本地 aiohttp 测试服务器，验证完整抓包流程。

### 方式 1：在 sidecar 容器内运行（推荐）

构建并启动 sidecar 后:

```bash
docker compose -f docker-compose.local.yml up -d --build playwright

# 进入容器, 安装测试依赖, 跑测试
docker compose -f docker-compose.local.yml exec playwright sh -c "
    cd /app && \
    pip install -r requirements-dev.txt && \
    pytest -v
"
```

### 方式 2：宿主机运行

```bash
# 宿主机需要 Python 3.10+
cd services/playwright
pip install -r requirements.txt -r requirements-dev.txt
pip install playwright==1.49.0
playwright install chromium    # 下载浏览器约 200MB

pytest -v
```

### 方式 3：单独执行某个用例

```bash
pytest test_integration.py::test_heuristic_finds_signin_button -v
```

## 集成测试覆盖场景

| 用例 | 验证 |
| --- | --- |
| `test_heuristic_finds_signin_button` | 启发式找到 `[data-testid]` 按钮并点击, HAR 含 POST /api/sign |
| `test_hint_overrides_default_priority` | 提示词 `每日打卡` 优先选 `<a>` 而非 `<button>` |
| `test_explicit_selector_wins` | 用户传 selector 时跳过启发式直接点击 |
| `test_explicit_selector_not_found_returns_error` | selector 不存在时友好报错 |
| `test_cookie_string_attached` | Cookie 字符串注入后请求自带 `Cookie:` header |
| `test_storage_state_failure_detected` | 无 cookie 访问受保护页 → 重定向到 /login → 返回错误 |
| `test_cross_domain_storage_state_dropped` | 跨域 cookie 在 sanitize 阶段被剔除 |
| `test_no_button_returns_candidates` | 页面没签到按钮时返回候选列表 |
| `test_har_contains_post_after_click` | 点击后等待 `wait_after_click_ms` 期间的请求都被记录 |

## 离线 / CI 行为

`conftest.py` 用 `pytest.importorskip` 自动跳过缺 `playwright` / `pytest-asyncio` / `aiohttp` 的环境。所以:

- 仓库 CI 默认只跑 `test_button_finder.py` 的纯逻辑用例（无浏览器开销）
- 想跑完整集成需要单独配置一个带 playwright 镜像的 job

## 调试

集成测试中浏览器以 headless 模式运行。本地复现某个失败时:

```python
# 临时改 conftest.py 里的 browser fixture:
b = await pw.chromium.launch(headless=False, slow_mo=500)
```

或在测试中给某个 page 加 `await page.pause()`，然后用 `PWDEBUG=1 pytest ...` 启动 Playwright Inspector。
