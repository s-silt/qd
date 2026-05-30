"""QD 内置 Playwright 抓包模块。

提供 URL 自动抓包功能，无需外部 sidecar 服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
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


async def init_browser():
    """初始化 Playwright 浏览器（懒加载）。"""
    global _browser, _playwright_ctx, _semaphore, _initialized

    if _initialized and _browser and _browser.is_connected():
        return

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright 未安装，请运行: pip install playwright && playwright install chromium"
        )

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


async def close_browser():
    """关闭 Playwright 浏览器。"""
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
    logger.info("Playwright 浏览器已关闭")


def validate_url(url: str) -> str:
    """验证 URL 是否合法。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("URL 必须是 http(s)://")
    if not u.hostname:
        raise ValueError("URL 缺少 hostname")
    host = u.hostname.lower()
    if PLAYWRIGHT_ALLOW_HOSTS:
        if not any(host == h or host.endswith("." + h) for h in PLAYWRIGHT_ALLOW_HOSTS):
            raise ValueError(f"hostname 不在 PLAYWRIGHT_ALLOW_HOSTS 白名单内: {host}")
    else:
        # 未配置白名单时, 仍拦截环回 / 链路本地(云元数据) / 多播等内部地址防 SSRF
        reason = resolve_blocked_reason(host)
        if reason:
            raise ValueError(f"目标地址被 SSRF 防护拦截: {reason}")
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
            await context.add_init_script(STEALTH_INIT_JS)
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)

            try:
                actions.append({"type": "navigate", "url": url})
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    actions.append({"type": "navigate_timeout"})

                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                # 检查是否被重定向到登录页
                current = page.url
                if re.search(r"login|signin|sign-in|auth", current, re.I) and not re.search(
                    r"login|signin|sign-in|auth", url, re.I
                ):
                    await context.close()
                    return {
                        "ok": False,
                        "error": f"页面被重定向到 {current}, 登录态可能已失效",
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
                        await context.close()
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
                        await context.close()
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

                await context.close()

                # 读取 HAR
                with open(har_path, "r", encoding="utf-8") as f:
                    har_data = json.load(f)

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
                try:
                    await context.close()
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": f"内部异常: {e}",
                    "actions": actions,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            finally:
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
    """
    await init_browser()

    if not _browser or not _browser.is_connected():
        return {"ok": False, "error": "浏览器未就绪"}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ok": False, "error": "playwright 未安装"}

    started = time.time()

    try:
        # 创建浏览器上下文（不录制 HAR）
        context = await _browser.new_context(
            user_agent=user_agent or DEFAULT_UA,
            viewport=viewport or {"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        await context.add_init_script(STEALTH_INIT_JS)
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        # 导航到目标页面
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 等待用户登录（检测 URL 变化或页面内容变化）
        # 这里我们给用户足够的时间手动登录
        await asyncio.sleep(min(timeout_ms / 1000, 120))  # 最多等待 120 秒

        # 获取 storage_state（包含 cookies）
        storage_state = await context.storage_state()

        # 提取当前域名的 cookies
        domain = urlparse(url).hostname or ""
        cookies_list = []
        for cookie in storage_state.get("cookies", []):
            cookie_domain = cookie.get("domain", "").lstrip(".")
            if cookie_domain == domain or domain.endswith("." + cookie_domain):
                cookies_list.append(f"{cookie['name']}={cookie['value']}")

        cookie_str = "; ".join(cookies_list)

        await context.close()

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


def is_available() -> bool:
    """检查 Playwright 是否可用。"""
    try:
        import playwright
        return True
    except ImportError:
        return False
