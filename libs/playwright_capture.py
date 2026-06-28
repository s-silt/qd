"""QD 内置 Playwright 抓包模块。

提供 URL 自动抓包功能，无需外部 sidecar 服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from libs.button_finder import JS_FIND_CANDIDATES, pick_button
from libs.security import (
    parse_cookie_str_to_storage_state,
    resolve_blocked_reason,
    sanitize_storage_state,
)

logger = logging.getLogger("qd.playwright")

# 配置
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() not in (
    "0",
    "false",
    "no",
)
PLAYWRIGHT_MAX_CONCURRENT = int(os.getenv("PLAYWRIGHT_MAX_CONCURRENT", "2"))
PLAYWRIGHT_DEFAULT_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_DEFAULT_TIMEOUT_MS", "60000"))
PLAYWRIGHT_ALLOW_HOSTS = [
    h.strip()
    for h in os.getenv("PLAYWRIGHT_ALLOW_HOSTS", "").split(",")
    if h.strip()
]

# 反检测注入
STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 全局状态
_browser = None
_playwright_ctx = None
_semaphore = None
_initialized = False
_init_lock = None  # asyncio.Lock, 懒加载防并发重复 launch

# 事件驱动登录检测的轮询间隔(秒); 测试可调小
_LOGIN_POLL_INTERVAL = 1.0

# 探测页面是否存在登录表单(密码输入框)
_PASSWORD_FIELD_JS = "() => !!document.querySelector('input[type=password]')"

# 常见会话 / 凭据 cookie 名片段, 用于判断是否真正登录
SESSION_COOKIE_HINTS = (
    "session",
    "sessid",
    "sid",
    "token",
    "auth",
    "passport",
    "login",
    "sso",
    "jwt",
    "uid",
    "userid",
    "remember",
)


def _get_init_lock() -> "asyncio.Lock":
    """懒加载初始化锁。单线程事件循环下创建过程不会让出, 无需额外保护。"""
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


def _check_host_allowed(host: str) -> Optional[str]:
    """统一的目标 host 校验: 白名单 + IP 段拦截【叠加】。

    返回拦截原因字符串; 放行返回 None。

    与旧实现不同: 即便配置了 PLAYWRIGHT_ALLOW_HOSTS 白名单, 仍叠加
    resolve_blocked_reason 的 IP 段检查, 防止白名单域名经 DNS-rebinding /
    重定向解析到内网 / 云元数据地址。
    """
    if not host:
        return "缺少 hostname"
    h = host.strip("[]").lower()
    if PLAYWRIGHT_ALLOW_HOSTS:
        if not any(h == a or h.endswith("." + a) for a in PLAYWRIGHT_ALLOW_HOSTS):
            return f"hostname 不在 PLAYWRIGHT_ALLOW_HOSTS 白名单内: {h}"
    reason = resolve_blocked_reason(h)
    if reason:
        return reason
    return None


async def _route_guard(route) -> None:
    """page.route 处理器: 对每一跳(含重定向 Location / 子资源)做白名单 +
    IP 段拦截, 命中内网 / 云元数据则 abort, 否则放行。"""
    try:
        req_url = route.request.url
    except Exception:
        req_url = ""
    host = urlparse(req_url).hostname or ""
    reason = _check_host_allowed(host)
    if reason:
        logger.warning("[security] 抓包拦截请求 %s: %s", req_url, reason)
        try:
            await route.abort()
        except Exception:
            pass
        return
    try:
        await route.continue_()
    except Exception:
        pass


def _strip_internal_from_har(har_data: Dict[str, Any]) -> Dict[str, Any]:
    """剔除 HAR 中指向内网 / 受限地址的请求条目, 防止 record_har_content=embed
    把内网响应原样回传。原地修改并返回。"""
    try:
        entries = har_data["log"]["entries"]
    except (KeyError, TypeError):
        return har_data
    kept = []
    for e in entries:
        try:
            req_url = e["request"]["url"]
        except (KeyError, TypeError):
            kept.append(e)
            continue
        host = urlparse(req_url).hostname or ""
        if _check_host_allowed(host):
            logger.warning("[security] HAR 剔除内网响应 %s", req_url)
            continue
        kept.append(e)
    har_data["log"]["entries"] = kept
    return har_data


def _has_session_cookie(cookies: Optional[List[Dict[str, Any]]]) -> bool:
    """cookies 中是否存在像样的会话 / 凭据 cookie (有值)。"""
    for c in cookies or []:
        name = (c.get("name") or "").lower()
        if not (c.get("value") or ""):
            continue
        if any(h in name for h in SESSION_COOKIE_HINTS):
            return True
    return False


def _same_site(host_a: str, host_b: str) -> bool:
    """粗略判断两 host 是否同一注册站点(末两段相同)。"""
    a = (host_a or "").lower().split(".")
    b = (host_b or "").lower().split(".")
    if len(a) < 2 or len(b) < 2:
        return (host_a or "").lower() == (host_b or "").lower()
    return a[-2:] == b[-2:]


def _is_login_redirect(
    original_url: str,
    current_url: str,
    has_password_field: bool,
    has_session_cookie: bool,
) -> bool:
    """判断当前页面是否为「被重定向到登录页」。

    取代旧的 login|signin|auth 正则(会误杀正常站点 / 漏判真重定向)。
    综合信号: host(站点) 变化 + 是否有会话 cookie + 是否出现登录表单字段。
    """
    orig_host = (urlparse(original_url).hostname or "").lower()
    cur_host = (urlparse(current_url).hostname or "").lower()
    cross_site = bool(orig_host and cur_host and not _same_site(orig_host, cur_host))

    # 已有会话 cookie 且没有登录表单 -> 视为已登录, 不算重定向
    if has_session_cookie and not has_password_field:
        return False
    # 跳到不同站点(典型 SSO/IdP) 且出现登录表单
    if cross_site and has_password_field:
        return True
    # 同站但出现登录表单且没有会话 cookie -> 登录页
    if has_password_field and not has_session_cookie:
        return True
    return False


async def _collect_login_signals(page, context):
    """采集登录判定所需信号: (当前 url, 是否有密码框, cookies 列表)。"""
    try:
        has_pw = bool(await page.evaluate(_PASSWORD_FIELD_JS))
    except Exception:
        has_pw = False
    try:
        state = await context.storage_state()
        cookies = state.get("cookies", []) or []
    except Exception:
        cookies = []
    cur_url = getattr(page, "url", "") or ""
    return cur_url, has_pw, cookies


async def init_browser():
    """初始化 Playwright 浏览器（懒加载, 加锁防并发重复 launch）。"""
    global _browser, _playwright_ctx, _semaphore, _initialized

    if _initialized and _browser and _browser.is_connected():
        return

    async with _get_init_lock():
        # 双重检查: 可能已被并发的另一协程初始化
        if _initialized and _browser and _browser.is_connected():
            return

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright 未安装，请运行: pip install playwright && playwright install chromium"
            )

        # 信号量只建一次, 避免孤立旧实例 / 容量漂移
        if _semaphore is None:
            _semaphore = asyncio.Semaphore(PLAYWRIGHT_MAX_CONCURRENT)

        logger.info(
            "初始化 Playwright (headless=%s, concurrent=%d)",
            PLAYWRIGHT_HEADLESS,
            PLAYWRIGHT_MAX_CONCURRENT,
        )

        if not PLAYWRIGHT_ALLOW_HOSTS:
            logger.warning(
                "[security] PLAYWRIGHT_ALLOW_HOSTS 未设置, 将接受任意 hostname。"
                " 部署到生产环境时请设置 PLAYWRIGHT_ALLOW_HOSTS=example.com,foo.com 防 SSRF。"
            )

        # 关闭可能存在的孤立旧实例后再重建
        if _browser is not None or _playwright_ctx is not None:
            await _teardown_browser()

        _playwright_ctx = await async_playwright().start()
        _browser = await _playwright_ctx.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        _initialized = True
        logger.info("Playwright 浏览器就绪")


async def _teardown_browser():
    """关闭并清理浏览器 / playwright 实例(不动 _semaphore / _init_lock)。"""
    global _browser, _playwright_ctx, _initialized
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
    if _playwright_ctx:
        try:
            await _playwright_ctx.stop()
        except Exception:
            pass
    _browser = None
    _playwright_ctx = None
    _initialized = False


async def close_browser():
    """关闭 Playwright 浏览器。"""
    await _teardown_browser()
    logger.info("Playwright 浏览器已关闭")


def validate_url(url: str) -> str:
    """验证 URL 是否合法。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("URL 必须是 http(s)://")
    if not u.hostname:
        raise ValueError("URL 缺少 hostname")
    # 白名单 + IP 段拦截叠加(见 _check_host_allowed)
    reason = _check_host_allowed(u.hostname)
    if reason:
        raise ValueError(f"目标地址被拦截: {reason}")
    return url


async def _find_and_click(page, hint: str, actions: List[Dict[str, Any]]):
    """启发式找签到按钮并点击。返回 (是否点中, 选中的按钮, 候选 top 10)。"""
    try:
        from playwright.async_api import Error as PlaywrightError
    except ImportError:
        return False, None, []

    candidates = await page.evaluate(JS_FIND_CANDIDATES)
    chosen, top = pick_button(candidates, hint=hint)
    if not chosen:
        return False, None, top

    # 模拟人类延迟
    await asyncio.sleep(0.1 + random.random() * 0.3)
    selector = chosen["selector"]
    try:
        await page.click(selector, timeout=10000)
    except Exception:
        # selector 失效, 退化到按文本点击
        text = chosen["text"]
        try:
            await page.get_by_text(text, exact=False).first.click(timeout=10000)
        except Exception as e:
            actions.append({"type": "click_failed", "selector": selector, "error": str(e)})
            return False, chosen, top

    actions.append({"type": "click", "selector": selector, "text": chosen["text"]})
    return True, chosen, top


async def capture_har(
    url: str,
    cookies: Optional[str] = None,
    storage_state: Optional[Dict[str, Any]] = None,
    hint: str = "",
    selector: Optional[str] = None,
    user_agent: Optional[str] = None,
    viewport: Optional[Dict[str, int]] = None,
    timeout_ms: int = PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
    wait_after_click_ms: int = 3000,
) -> Dict[str, Any]:
    """抓取 URL 的 HAR 数据。

    Args:
        url: 目标 URL
        cookies: Cookie 字符串 "k1=v1; k2=v2"
        storage_state: Playwright storage_state JSON
        hint: 提示词，帮助匹配签到按钮
        selector: 用户指定的 CSS selector
        user_agent: 自定义 UA
        viewport: 视口大小 {"width": 1280, "height": 800}
        timeout_ms: 页面加载超时
        wait_after_click_ms: 点击后等待时间

    Returns:
        {"ok": True, "har": {...}, "actions": [...], ...} 或
        {"ok": False, "error": "...", ...}
    """
    try:
        from playwright.async_api import Error as PlaywrightError
    except ImportError:
        return {"ok": False, "error": "playwright 未安装"}

    await init_browser()

    if not _browser or not _browser.is_connected():
        return {"ok": False, "error": "浏览器未就绪"}

    started = time.time()
    actions: List[Dict[str, Any]] = []

    # 处理 cookies
    if not storage_state and cookies:
        storage_state = parse_cookie_str_to_storage_state(cookies, url)
    if storage_state:
        storage_state = sanitize_storage_state(storage_state, url)

    # 创建临时 HAR 文件
    har_fd, har_path = tempfile.mkstemp(suffix=".har", prefix="qd_capture_")
    os.close(har_fd)

    try:
        # 并发限制
        async with _semaphore:
            context = await _browser.new_context(
                user_agent=user_agent or DEFAULT_UA,
                viewport=viewport or {"width": 1280, "height": 800},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                storage_state=storage_state,
                record_har_path=har_path,
                record_har_content="embed",
            )
            try:
                # 每一跳(导航 / 重定向 Location / 子资源)都过 SSRF 白名单 + IP 段
                await context.route("**/*", _route_guard)
                await context.add_init_script(STEALTH_INIT_JS)
                page = await context.new_page()
                page.set_default_timeout(timeout_ms)

                actions.append({"type": "navigate", "url": url})
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    actions.append({"type": "navigate_timeout"})

                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                # 检查是否被重定向到登录页(host 变化 + 会话 cookie + 登录表单)
                current, has_pw, cur_cookies = await _collect_login_signals(page, context)
                if _is_login_redirect(
                    url, current, has_pw, _has_session_cookie(cur_cookies)
                ):
                    return {
                        "ok": False,
                        "error": f"页面被重定向到登录页 {current}, 登录态可能已失效",
                        "actions": actions,
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

                # 点击签到按钮
                if selector:
                    try:
                        await page.click(selector, timeout=10000)
                        actions.append({"type": "click", "selector": selector, "manual": True})
                        chosen = {"selector": selector, "text": "(用户指定)"}
                        candidates: List[Dict[str, Any]] = []
                    except Exception as e:
                        return {
                            "ok": False,
                            "error": f"用户指定的 selector 点击失败: {e}",
                            "actions": actions,
                            "elapsed_ms": int((time.time() - started) * 1000),
                        }
                else:
                    clicked, chosen, candidates = await _find_and_click(
                        page, hint, actions
                    )
                    if not clicked:
                        return {
                            "ok": False,
                            "error": "未找到匹配的签到按钮, 请检查 hint 或手动指定 selector",
                            "actions": actions,
                            "candidates": candidates,
                            "elapsed_ms": int((time.time() - started) * 1000),
                        }

                # 等待响应
                await asyncio.sleep(wait_after_click_ms / 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

                # 必须先关闭 context, HAR 才会落盘
                await context.close()
                context = None

                # 读取 HAR 并剔除内网响应(防 embed 回传内网内容)
                with open(har_path, "r", encoding="utf-8") as f:
                    har_data = json.load(f)
                har_data = _strip_internal_from_har(har_data)

                return {
                    "ok": True,
                    "har": har_data,
                    "actions": actions,
                    "found_button": chosen,
                    "candidates": candidates,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

            except Exception as e:
                logger.exception("抓包失败: %s", e)
                return {
                    "ok": False,
                    "error": f"内部异常: {e}",
                    "actions": actions,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            finally:
                # context + 临时 HAR 文件 try/finally 兜底, 不泄漏
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                try:
                    os.unlink(har_path)
                except OSError:
                    pass

    except Exception as e:
        logger.exception("抓包初始化失败: %s", e)
        return {
            "ok": False,
            "error": f"初始化失败: {e}",
            "actions": actions,
            "elapsed_ms": int((time.time() - started) * 1000),
        }


async def capture_cookies(
    url: str,
    user_agent: Optional[str] = None,
    viewport: Optional[Dict[str, int]] = None,
    timeout_ms: int = 120000,
) -> Dict[str, Any]:
    """打开网页让用户登录，然后抓取 cookies。

    Args:
        url: 目标 URL
        user_agent: 自定义 UA
        viewport: 视口大小
        timeout_ms: 等待用户登录的超时时间

    Returns:
        {"ok": True, "cookies": "k1=v1; k2=v2", "storage_state": {...}} 或
        {"ok": False, "error": "..."}

    注意: 无头(headless)模式下没有人可以手动登录, 若在超时内未检测到登录完成,
    明确返回 ok=False(而非盲睡 120s 后把游客 cookie 当成功返回), 并提示改用
    浏览器扩展在本机抓取。
    """
    try:
        await init_browser()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    if not _browser or not _browser.is_connected():
        return {"ok": False, "error": "浏览器未就绪"}

    started = time.time()
    # 事件驱动登录检测, 保留超时上限(最多 120s)
    deadline = started + min(max(timeout_ms, 0) / 1000.0, 120.0)

    # 纳入信号量, 避免独占浏览器并发名额
    async with _semaphore:
        context = await _browser.new_context(
            user_agent=user_agent or DEFAULT_UA,
            viewport=viewport or {"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        try:
            await context.add_init_script(STEALTH_INIT_JS)
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 记录初始 cookie 名, 用于判断「登录后新出现的会话 cookie」
            try:
                init_state = await context.storage_state()
                initial_names = {
                    c.get("name") for c in init_state.get("cookies", []) or []
                }
            except Exception:
                initial_names = set()

            logged_in = False
            # 事件驱动轮询: 检测到登录完成即退出, 否则等到超时
            while True:
                cur_url, has_pw, cookies = await _collect_login_signals(page, context)
                new_session = any(
                    (c.get("name") not in initial_names)
                    and (c.get("value") or "")
                    and any(
                        h in (c.get("name") or "").lower()
                        for h in SESSION_COOKIE_HINTS
                    )
                    for c in cookies
                )
                # 登录完成判据: 无密码框 + 存在会话 cookie + 未停留在登录重定向
                if (
                    not has_pw
                    and (new_session or _has_session_cookie(cookies))
                    and not _is_login_redirect(
                        url, cur_url, has_pw, _has_session_cookie(cookies)
                    )
                ):
                    logged_in = True
                    break
                if time.time() >= deadline:
                    break
                await asyncio.sleep(min(_LOGIN_POLL_INTERVAL, max(0.0, deadline - time.time())))

            if not logged_in:
                if PLAYWRIGHT_HEADLESS:
                    err = (
                        "无头模式下无法手动登录, 未检测到登录完成。"
                        "请在本机用浏览器扩展登录后抓取 cookie 再导入"
                        "(或设置 PLAYWRIGHT_HEADLESS=false 以可视化登录)。"
                    )
                else:
                    err = "等待登录超时, 未检测到登录完成。"
                return {
                    "ok": False,
                    "error": err,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

            # 获取并净化 storage_state(剔除跨域 SSO/支付令牌)
            storage_state = await context.storage_state()
            storage_state = sanitize_storage_state(storage_state, url)

            domain = urlparse(url).hostname or ""
            cookies_list = [
                f"{c['name']}={c['value']}"
                for c in storage_state.get("cookies", [])
            ]
            cookie_str = "; ".join(cookies_list)

            return {
                "ok": True,
                "cookies": cookie_str,
                "storage_state": storage_state,
                "domain": domain,
                "cookie_count": len(cookies_list),
                "elapsed_ms": int((time.time() - started) * 1000),
            }

        except Exception as e:
            logger.exception("Cookie 抓取失败: %s", e)
            return {
                "ok": False,
                "error": f"抓取失败: {e}",
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        finally:
            # context try/finally 兜底, 不泄漏
            try:
                await context.close()
            except Exception:
                pass


def is_available() -> bool:
    """检查 Playwright 是否可用。"""
    try:
        import playwright
        return True
    except ImportError:
        return False
