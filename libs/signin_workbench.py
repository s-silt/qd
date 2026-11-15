"""Browser recording, replay and credential-free capture summaries."""

import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import config
from libs import playwright_capture, signin_pcbeta
from libs.browser_egress import BrowserEgress, public_addresses
from libs.button_finder import JS_FIND_CANDIDATES

VIEWPORT = {"width": 1100, "height": 740}
SESSION_SECONDS = 600
MAX_RECORDS = 60
MAX_STEPS = 6
MAX_SESSION_BYTES = 2 * 1024 * 1024
EXAMPLE_KEYS = {
    hashlib.sha256(value).digest() for value in (b"binux", b"please-change-to-a-long-random-string")
}
AUTH_FIELD = re.compile(
    r"(?i)pass(?:word|wd)?|cookie|authorization|secret|credential|session|token|csrf|"
    r"otp|captcha|sms|(?:^|[_-])(?:pin|code|phone|mobile|email|account|username|key)(?:$|[_-])"
)
AUTH_PATH = re.compile(
    r"(?i)(?:^|[/_.-])(?:login|log[-_]?in|logout|sign[-_]in|auth|oauth\w*|sso|passport|"
    r"captcha|verify|verification|challenge|sms|otp|qrconnect)(?:$|[/_.-])"
)
_PUBLIC_PLUGIN_PATH = re.compile(r"/(?:[a-zA-Z][a-zA-Z0-9_-]{0,39}/){0,5}plugin\.php")
_PUBLIC_TASK_LANDING = re.compile(r"/(?:[a-zA-Z][a-zA-Z0-9_-]{0,39}/){0,5}home\.php")
_PUBLIC_PLUGIN_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}(?::[a-zA-Z][a-zA-Z0-9_-]{0,39})?")
_PUBLIC_ROUTE_KEY = frozenset({"id", "mod", "action", "do", "plugin", "page"})
_PUBLIC_SIGNIN_WORD = re.compile(
    r"(?:^|[_-])(?:check[-_]?in|attendance|qiandao|sign|qd|punch)(?:$|[_-])", re.I
)
_PUBLIC_SIGNIN_ROUTE = frozenset({"attendance", "check-in", "check_in", "checkin", "punch", "qiandao", "sign"})
_PUBLIC_ROUTING_VALUE = _PUBLIC_SIGNIN_ROUTE | {"action", "daily", "index", "qd"}
_PRIVATE_ROUTE_WORD = re.compile(
    r"(?:^|[:/_.-])(?:security|challenge|verification|authenticat\w*|authorize|register|reset|"
    r"passwd|password|session|token|secret|credential|cookie|captcha|csrf|turnstile|recaptcha|"
    r"hcaptcha|geetest)(?:$|[:/_.-])", re.I
)
SIGNIN_PATH = re.compile(r"(?i)check[-_]?in|attendance|qiandao|daily[-_]?sign|punch|签到|打卡")
VERIFICATION_FRAME = re.compile(r"(?i)captcha|challenge|qrconnect|open\.weixin\.qq\.com|recaptcha|turnstile")
SENSITIVE_VALUE = re.compile(
    r"(?i)bearer\s|sk-[\w-]{8,}|[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b1[3-9]\d{9}\b|[\w+/=-]{36,}"
)
CHALLENGE = re.compile(
    r"(?:输入|获取|发送).{0,6}验证码|安全验证|滑动.{0,8}(滑块|验证)|请.{0,6}登录|登录.{0,5}(失效|过期)|"
    r"(?:请|等待).{0,8}(微信|扫码)|just a moment|verify you are human|security check|"
    r"captcha required|login required|unauthori[sz]ed|token expired",
    re.I,
)
CLOUDFLARE_VERIFICATION = re.compile(
    r"performing\s+security\s+verification.{0,600}verifies\s+you\s+are\s+not\s+a\s+bot",
    re.I | re.S,
)
SUCCESS = re.compile(
    r"签到成功|(?<!未)(?:已签到|已经签到)|重复签到|打卡成功|(?<!未)已打卡|checked\s*in|already\s+checked",
    re.I,
)
CLICK_CANDIDATE = re.compile(
    r"(?:立即|每日|今日|去|点击)?(?:签到|打卡)|(?:开始|继续)(?:签到|打卡)(?:任务)?|"
    r"(?:daily )?check[ -]?in|(?:start|continue) (?:daily )?check[ -]?in",
    re.I,
)
CLAIM_CHECKIN = re.compile(r"领取(?:签到|打卡)奖励|(?:签到|打卡)领取奖励|claim check[ -]?in reward", re.I)
AUTO_NO_BUTTON = "没有可明确识别的签到按钮"
GOTO_TIMEOUT = 15000
_replay_slots = asyncio.Semaphore(max(1, playwright_capture.PLAYWRIGHT_MAX_CONCURRENT))
_pcbeta_lock = asyncio.Lock()
_browser_lock = asyncio.Lock()
_browser = None
_driver = None


class WorkbenchError(Exception):
    pass


class VerificationNeeded(WorkbenchError):
    pass


class WorkbenchPermissionError(WorkbenchError):
    def __init__(self, status_code=403, message="没有权限"):
        super().__init__(message)
        self.status_code = status_code


def browser_enabled():
    return (
        config.ai_workbench_browser
        and not config.multiprocess
        and playwright_capture.is_available()
        and config.cookie_secret not in EXAMPLE_KEYS
        and config.aes_key not in EXAMPLE_KEYS
    )


def web_origin(url):
    if (
        not isinstance(url, str)
        or len(url) > 4096
        or "\\" in url
        or any(ord(c) < 33 or ord(c) == 127 for c in url)
    ):
        raise ValueError("请输入完整的 HTTP(S) 网站地址")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
        raise ValueError("请输入完整的 HTTP(S) 网站地址")
    if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
        raise ValueError("在线浏览器使用网站的标准 HTTP(S) 端口")
    host = parsed.hostname.encode("idna").decode().rstrip(".")
    if not host or len(host) > 253 or "%" in host:
        raise ValueError("网址的主机名无效")
    return f"{parsed.scheme}://[{host}]" if ":" in host else f"{parsed.scheme}://{host}"


def page_label(url):
    try:
        return web_origin(url)
    except ValueError:
        return "新标签页"


def capture_summary(url, method, resource, status):
    """Allowlisted metadata only, with dynamic paths and query values discarded."""
    parsed = urlsplit(url)
    path = unquote(parsed.path or "/")
    query = parse_qsl(parsed.query, max_num_fields=50)
    if AUTH_PATH.search(path) or any(AUTH_FIELD.search(key) for key, _ in query):
        return None
    parts = path.split("/")[:12]
    # Only fixed vocabulary leaves the server. Opaque paths can contain credentials.
    safe_words = {
        "api",
        "checkin",
        "check-in",
        "attendance",
        "daily",
        "signin",
        "sign",
        "qiandao",
        "punch",
        "status",
        "points",
        "reward",
        "user",
        "profile",
        "task",
        "签到",
        "打卡",
    }
    sanitized = "/".join(
        part.lower() if not part or part.lower() in safe_words else "{value}" for part in parts
    )
    method = method if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD") else "OTHER"
    return dict(
        method=method,
        path=sanitized or "/",
        type=resource,
        status=status,
        signin_hint=bool(SIGNIN_PATH.search(path)),
    )


def clean_chat(text):
    from libs.ai.redact import redact_secrets

    text = redact_secrets(text)
    text = re.sub(r"(?i)(?:https?://)[^\s<>]+", lambda m: page_label(m.group()), text)
    text = re.sub(
        r"(?i)(?:\b(?:cookie|authorization|password|passwd|otp)|验证码|密码)\s*[:：=]\s*[^\n]+",
        "[已脱敏]",
        text,
    )
    text = re.sub(r"\b\d{4,8}\b", "[已脱敏]", text)
    return SENSITIVE_VALUE.sub("[已脱敏]", text)


def credentials_in_chat(text):
    return bool(
        SENSITIVE_VALUE.search(text)
        or re.search(
            r"(?i)(?:cookie|authorization|password|passwd|otp|验证码|密码)\s*[:：=]\s*\S|\b\d{4,8}\b",
            text,
        )
    )


def validate_steps(origin, start_url, steps):
    """The replay script consists of observed clicks, never model-provided code."""
    error = _recording_url_error(origin, start_url)
    if error:
        raise WorkbenchError(error)
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise WorkbenchError("请录制 1 到 6 次签到相关点击")
    if not all(
        isinstance(step, dict)
        and isinstance(step.get("selector"), str)
        and 0 < len(step["selector"]) <= 420
        and isinstance(step.get("text"), str)
        and step.get("tag") in ("button", "a", "input", "div", "span", "label", "li")
        for step in steps
    ):
        raise WorkbenchError("签到操作中有无法回放的点击")


def validate_script(script):
    if not isinstance(script, dict) or not {"origin", "url", "steps"} <= script.keys():
        raise WorkbenchError("签到脚本格式无效")
    if "recipe" in script:
        if script != pcbeta_daily_script():
            raise WorkbenchError("远景论坛每日打卡脚本格式无效")
        return script
    validate_steps(script["origin"], script["url"], script["steps"])
    labels = script.get("labels")
    if labels is not None and (
        not isinstance(labels, list) or len(labels) != len(script["steps"])
        or not all(isinstance(label, str) and len(label) <= 80 for label in labels)
    ):
        raise WorkbenchError("签到脚本的步骤说明格式无效")
    return script


def pcbeta_daily_script():
    return {
        "recipe": signin_pcbeta.RECIPE,
        "origin": signin_pcbeta.ORIGIN,
        "url": signin_pcbeta.START_URL,
        "steps": [{"text": name} for name in signin_pcbeta.STEP_TEXTS],
        "labels": ["任务列表", "任务详情", "签到回执"],
    }


def is_public_signin_query(path, query):
    """Recognize supported public routing on forum sign-in and task landing pages."""
    if not query or len(query) > 256:
        return False
    if AUTH_PATH.search(path) or _PRIVATE_ROUTE_WORD.search(path):
        return False
    if query == "mod=task" and _PUBLIC_TASK_LANDING.fullmatch(path):
        return True
    if not _PUBLIC_PLUGIN_PATH.fullmatch(path):
        return False
    seen = set()
    for part in query.split("&"):
        key, sep, raw = part.partition("=")
        if not sep or key in seen or key not in _PUBLIC_ROUTE_KEY:
            return False
        if key == "id":
            value = re.sub(r"%3[aA]", ":", raw)
            if (
                not _PUBLIC_PLUGIN_ID.fullmatch(value) or _PRIVATE_ROUTE_WORD.search(value)
                or AUTH_PATH.search(value.replace(":", "/"))
            ):
                return False
            module, _, route = value.lower().partition(":")
            if route and route not in _PUBLIC_ROUTING_VALUE:
                return False
            if not _PUBLIC_SIGNIN_WORD.search(module) and route not in _PUBLIC_SIGNIN_ROUTE:
                return False
            if AUTH_FIELD.search(module) or SENSITIVE_VALUE.search(module) or re.search(r"[a-z0-9]{21}|\d{4}", module):
                return False
        elif raw not in _PUBLIC_ROUTING_VALUE:
            return False
        seen.add(key)
    return "id" in seen


def _recording_url_error(origin, url):
    try:
        actual = web_origin(url)
    except ValueError:
        return "请在浏览器里打开有效的签到页面，再开始录制"
    same = origin == actual or (
        isinstance(origin, str) and origin.startswith("http://") and actual == "https://" + origin[7:]
    )
    if not same:
        return "请回到最初打开的网站，再从该站点的签到页面开始录制"
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    if AUTH_PATH.search(path) or _PRIVATE_ROUTE_WORD.search(path):
        return "请在浏览器里完成登录和验证，再打开签到页面开始录制"
    if parsed.query and not is_public_signin_query(parsed.path, parsed.query):
        return "此页面带有工作台无法保存的网址参数。请从网站菜单打开普通签到页，再开始录制"
    return ""


def can_record(origin, url):
    return not _recording_url_error(origin, url)


@dataclass
class Recording:
    origin: str
    start_url: str
    steps: list = field(default_factory=list)
    summaries: list = field(default_factory=list)
    blocked: int = 0
    needs_verification: bool = False
    success: bool = False
    initial_success: str = ""
    invalid: bool = False
    auto_started: bool = False


class PageVerification(VerificationNeeded):
    def __init__(self, message, state):
        super().__init__(message)
        self.state = state


async def challenge_visible(page):
    if AUTH_PATH.search(urlsplit(page.url).path):
        return True
    if await page.locator(
        'input[type="password"]:visible, input[autocomplete="one-time-code"]:visible'
    ).count():
        return True
    from playwright.async_api import Error as BrowserError

    for frame in page.frames[1:]:
        if VERIFICATION_FRAME.search(frame.url):
            with suppress(BrowserError):
                element = await frame.frame_element()
                bounds = await element.bounding_box()
                if bounds and bounds["width"] >= 120 and bounds["height"] >= 90:
                    return True
    # Visible challenge text near the top or in a dialog. The text never leaves QD.
    text = (await page.locator("body").inner_text(timeout=2500))[:2200]
    return bool(CHALLENGE.search(text) or CLOUDFLARE_VERIFICATION.search(text))


async def success_signal(page):
    return await page.evaluate(
        r"""
      ({pattern, flags}) => {
        const result = [];
        const matcher = new RegExp(pattern, flags);
        const reader = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        for (let node, i = 0; i < 3000 && result.length < 8 && (node = reader.nextNode()); i++) {
          const text = node.textContent.trim();
          if (!text || text.length > 240 || /尚未|还未|未签到|not (?:yet )?checked/i.test(text)) continue;
          const match = text.match(matcher);
          if (!match || !node.parentElement?.getClientRects().length || getComputedStyle(node.parentElement).visibility === 'hidden') continue;
          result.push(match[0].toLowerCase());
        }
        return result.join('|');
      }
    """,
        {"pattern": SUCCESS.pattern, "flags": "i"},
    )


@asynccontextmanager
async def isolated_browser(**options):
    if not playwright_capture.is_available():
        raise WorkbenchError("Chromium 未安装，请运行 playwright install chromium")
    egress = BrowserEgress()
    try:
        proxy = await egress.start()
        browser = await interactive_browser()
        context = await browser.new_context(
            viewport=VIEWPORT,
            proxy=proxy,
            service_workers="block",
            accept_downloads=False,
            **options,
        )
        try:
            yield context
        finally:
            await context.close()
    finally:
        await egress.close()


async def interactive_browser():
    from playwright.async_api import async_playwright

    global _browser, _driver
    async with _browser_lock:
        if not _browser or not _browser.is_connected():
            if _driver:
                await _driver.stop()
            _driver = await async_playwright().start()
            _browser = await _driver.chromium.launch(
                headless=playwright_capture.PLAYWRIGHT_HEADLESS,
                executable_path=playwright_capture.PLAYWRIGHT_CHROMIUM_PATH,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
        return _browser


async def validate_start_url(url):
    origin = web_origin(url)
    parsed = urlsplit(origin)
    try:
        await public_addresses(parsed.hostname, 443 if parsed.scheme == "https" else 80)
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise WorkbenchError("无法打开此网站。请检查网址、公网解析和站点白名单") from exc
    return origin


def _record_response(recording, response):
    request = response.request
    if request.resource_type not in ("document", "fetch", "xhr"):
        return
    try:
        if web_origin(request.url) != recording.origin:
            recording.blocked += 1
            return
        summary = capture_summary(request.url, request.method, request.resource_type, response.status)
    except ValueError:
        summary = None
    if summary is None:
        recording.blocked += 1
    elif len(recording.summaries) < MAX_RECORDS:
        summary["after_action"] = len(recording.steps)
        recording.summaries.append(summary)
    if response.status in (401, 403) and (request.method == "POST" or (summary and summary["signin_hint"])):
        recording.needs_verification = True


_FIND_CLICKED = """
({x, y}) => {
  const el = document.elementFromPoint(x, y)?.closest('button, a, [role=button], input[type=submit], input[type=button]');
  if (!el || el.disabled || el.type === 'password') return null;
  if (el.id && el.id.length < 100) return '#' + CSS.escape(el.id);
  const testid = el.getAttribute('data-testid');
  if (testid && testid.length < 100) return '[data-testid="' + CSS.escape(testid) + '"]';
  const path = [];
  for (let node = el; node && node !== document.body; node = node.parentElement) {
    const siblings = Array.from(node.parentElement.children).filter(peer => peer.localName === node.localName);
    path.unshift(node.localName + ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')');
    if (path.length > 10) return null;
  }
  return 'body > ' + path.join(' > ');
}
"""

_FIND_TARGET = """
el => ({
  text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 160),
  tag: el.localName,
  href: el.localName === 'a' ? el.getAttribute('href') || '' : '',
})
"""

_AUTO_TARGET = """
el => ({
  link: el.matches('a[href]') ? {url: el.href, target: el.target} : null,
  form: el.form && el.type === 'submit' ? {
    url: el.hasAttribute('formaction') ? el.formAction : el.form.action,
    target: el.hasAttribute('formtarget') ? el.formTarget : el.form.target,
  } : null,
  baseTarget: document.querySelector('base[target]')?.target || '',
  disabled: Boolean(el.closest('[inert], [aria-disabled="true"]')),
  download: el.hasAttribute('download'),
})
"""

_AUTO_PROGRESS = r"""
({selector, text, url}) => {
  if (location.href !== url) return true;
  const el = document.querySelector(selector);
  if (!el || !el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') return true;
  const current = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
  return current.replace(/\s+/g, ' ') !== text;
}
"""


async def recorded_click(page, selector, origin):
    target = await page.locator(selector).evaluate(_FIND_TARGET, timeout=2500)
    href = target.pop("href")
    if href and not href.startswith("#"):
        target_url = urljoin(page.url, href)
        if page_label(target_url) != origin or AUTH_PATH.search(urlsplit(target_url).path):
            raise WorkbenchError("签到录制仅保存原站点的签到操作")
    return {"selector": selector, **target}


class BrowserSession:
    def __init__(self, owner, start_url, taskid=None):
        self.id = secrets.token_urlsafe(24)
        self.owner = owner
        self.start_url = start_url
        self.taskid = taskid
        self.templateid = None
        self.template_script = None
        self.lock = asyncio.Lock()
        self.expires = None
        self.context_manager = None
        self.context = None
        self.page = None
        self.recording = None
        self.captured = None
        self.draft = None
        self.saved = None
        self.created_task = None
        self.buttons = set()
        self.last_frame_at = 0.0
        self.frame_cache = None
        self.frame_page = None
        self.verification_probe = None

    async def open(self, state=None):
        self.context_manager = isolated_browser(storage_state=state)
        self.context = await self.context_manager.__aenter__()
        try:
            self.page = await self.context.new_page()
            self.context.on("page", self._new_tab)
            self.context.on("response", self._on_response)
            await self.goto(self.start_url)
        except BaseException:
            await self.close()
            raise

    def _new_tab(self, page):
        self.page = page
        if self.recording:
            self.recording.invalid = True

    def _on_response(self, response):
        if self.recording:
            _record_response(self.recording, response)

    def touch(self, expire):
        if self.expires:
            self.expires.cancel()
        self.expires = asyncio.get_running_loop().call_later(SESSION_SECONDS, expire)

    def active_page(self):
        if self.page and not self.page.is_closed():
            return self.page
        pages = self.context.pages if self.context else []
        if not pages:
            raise WorkbenchError("当前没有打开的浏览器标签页")
        self.page = pages[-1]
        return self.page

    async def goto(self, url):
        await validate_start_url(url)
        if self.recording:
            raise WorkbenchError("请先结束录制，再打开其他网址")
        if self.context and not self.context.pages:
            self.page = await self.context.new_page()
        from playwright.async_api import TimeoutError as BrowserTimeout

        page = self.active_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
        except BrowserTimeout:
            if "://" not in page_label(page.url):
                raise WorkbenchError("站点加载超时，请重试") from None

    async def begin(self):
        if self.recording is not None:
            raise WorkbenchError("请先结束当前录制")
        page = self.active_page()
        if await challenge_visible(page):
            raise VerificationNeeded("此页面仍需要验证。请完成验证并打开签到页面")
        url = urlsplit(page.url)
        start_url = url._replace(fragment="").geturl()
        error = _recording_url_error(web_origin(self.start_url), start_url)
        if error:
            raise WorkbenchError(error)
        origin = web_origin(start_url)
        self.recording = Recording(origin, start_url, initial_success=await success_signal(page))
        self.draft = self.captured = None
        self.saved = None
        self.created_task = None

    async def _check_auto_page(self, page, *, probe=True):
        recording = self.recording
        if recording.needs_verification:
            self.recording = None
            raise VerificationNeeded("站点要求再次登录或验证。请在浏览器里完成，确认当前签到状态后再录制")
        if recording.invalid or page.is_closed() or page_label(page.url) != recording.origin:
            self.recording = None
            raise WorkbenchError("签到操作打开了其他站点或标签页。请回到原站点，确认当前签到状态后再录制")
        if probe and (await challenge_visible(page) or recording.needs_verification):
            self.recording = None
            raise VerificationNeeded("站点要求再次登录或验证。请在浏览器里完成，确认当前签到状态后再录制")

    async def auto_click(self, page, *, confirmed, seen):
        from playwright.async_api import Error as BrowserError

        recording = self.recording
        ready = []
        try:
            for item in await page.evaluate(JS_FIND_CANDIDATES):
                text = " ".join(item["text"].split())
                if CLAIM_CHECKIN.fullmatch(text) or (not confirmed and CLICK_CANDIDATE.fullmatch(text)):
                    selector = item["selector"]
                    if len(selector) > 420:
                        return "签到按钮无法可靠定位"
                    locator = page.locator(selector)
                    if await locator.count() != 1:
                        return "签到按钮无法唯一定位"
                    if await locator.is_enabled():
                        ready.append((selector, locator, text))
                        if len(ready) > 1:
                            return "发现多个可用的签到按钮"
            if not ready:
                return AUTO_NO_BUTTON
            selector, locator, text = ready[0]
            key = (urlsplit(page.url).path, text.casefold())
            if key in seen:
                return "网页仍显示已执行过的签到步骤"
            await locator.click(trial=True, timeout=3500)
            await self._check_auto_page(page)
            target = await locator.evaluate(_AUTO_TARGET, timeout=2500)
            if target["disabled"] or target["download"]:
                return "签到按钮已变化或需要人工操作"
            for navigation in (target["link"], target["form"]):
                if navigation and (
                    page_label(navigation["url"]) != recording.origin
                    or AUTH_PATH.search(urlsplit(navigation["url"]).path)
                    or (navigation["target"] or target["baseTarget"]).lower() not in ("", "_self")
                ):
                    return "签到按钮会打开其他站点、标签页或登录验证页面"
            step = await recorded_click(page, selector, recording.origin)
            if " ".join(step["text"].split()) != text:
                return "签到按钮已变化或需要人工操作"
        except BrowserError:
            return "签到按钮已变化或需要人工操作"
        except WorkbenchError as exc:
            if self.recording is None:
                raise
            return str(exc)

        seen.add(key)
        recording.steps.append(step)
        before = page.url
        try:
            await locator.click(timeout=3500)
        except BrowserError:
            # The click may already have reached the site. Never repeat it automatically.
            if not any(row["after_action"] == len(recording.steps) for row in recording.summaries):
                recording.steps.pop()
            return "浏览器未能确认最后一次点击，请先查看站点当前状态"
        if not (recording.needs_verification or recording.invalid):
            # A document can already be network-idle while a SPA changes its next control.
            with suppress(BrowserError):
                await page.wait_for_function(
                    _AUTO_PROGRESS, arg={"selector": selector, "text": text, "url": before}, timeout=2000,
                )
        return ""

    async def auto_record(self):
        from playwright.async_api import Error as BrowserError

        recording = self.recording
        recording.auto_started = True
        page = self.active_page()
        seen = set()

        async def restrict_auto(route):
            request = route.request
            navigation = request.is_navigation_request()
            mutating = request.method not in ("GET", "HEAD", "OPTIONS")
            if navigation or mutating:
                if page_label(request.url) != recording.origin:
                    recording.invalid = True
                    await route.abort()
                    return
                if AUTH_PATH.search(unquote(urlsplit(request.url).path)):
                    recording.needs_verification = True
                    await route.abort()
                    return
            await route.fallback()

        await self.context.route("**/*", restrict_auto)
        try:
            for _ in range(MAX_STEPS):
                await self._check_auto_page(page)
                signal = await success_signal(page)
                if not recording.steps and already_checked(signal):
                    return "页面已显示签到状态，请在当前位置核对结果或手动继续"
                confirmed = bool(signal and (signal != recording.initial_success or already_checked(signal)))
                reason = await self.auto_click(page, confirmed=confirmed, seen=seen)
                if reason:
                    await self._check_auto_page(page)
                    if confirmed and reason == AUTO_NO_BUTTON:
                        return ""
                    return reason
                await settle(page)
            await self._check_auto_page(page)
            signal = await success_signal(page)
            if signal and (signal != recording.initial_success or already_checked(signal)):
                return ""
            return "已达到 6 步录制上限，请查看站点当前状态"
        except BrowserError:
            await self._check_auto_page(page, probe=False)
            return "网页正在变化，请查看站点当前状态"
        finally:
            await self.context.unroute("**/*", restrict_auto)

    async def finish_recording(self):
        if self.recording is None:
            raise WorkbenchError("请先开始录制签到操作")
        page = self.active_page()
        await settle(page)
        if self.recording.needs_verification or await challenge_visible(page):
            self.recording = None
            raise VerificationNeeded("站点在签到时要求再次验证。完成后请重新录制签到点击")
        recording = self.recording
        if recording.invalid or web_origin(page.url) != recording.origin:
            self.recording = None
            raise WorkbenchError("录制经过了其他站点或标签页。请回原站点重新录制签到步骤")
        validate_steps(recording.origin, recording.start_url, recording.steps)
        signal = await success_signal(page)
        recording.success = bool(signal and (signal != recording.initial_success or already_checked(signal)))
        self.recording = None
        self.captured = recording
        return recording

    async def verification_state(self, origin):
        page = self.active_page()
        await settle(page)
        if web_origin(page.url) != origin or await challenge_visible(page):
            raise VerificationNeeded("请完成验证并回到原站点的签到页面")
        return await persistable_state(self.context, origin)

    async def input(self, event):
        page = self.active_page()
        kind = event.get("kind")
        if kind == "tab":
            index = event.get("index")
            if type(index) is int and 0 <= index < len(self.context.pages):
                self.page = self.context.pages[index]
                if self.recording:
                    self.recording.invalid = True
            return
        if kind == "back":
            if self.recording:
                raise WorkbenchError("请结束录制后再返回上一页")
            await page.go_back(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
            return
        if kind == "reload":
            if self.recording:
                raise WorkbenchError("请结束录制后再刷新")
            await page.reload(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
            return
        if kind in ("move", "down", "up"):
            x, y = event.get("x"), event.get("y")
            if not (
                type(x) in (int, float)
                and type(y) in (int, float)
                and 0 <= x <= VIEWPORT["width"]
                and 0 <= y <= VIEWPORT["height"]
            ):
                raise WorkbenchError("无效的浏览器坐标")
            await page.mouse.move(x, y)
            if kind == "down":
                if "left" in self.buttons:
                    return
                self.buttons.add("left")
                if self.recording and not self.recording.invalid:
                    from playwright.async_api import Error as BrowserError

                    selector = await page.evaluate(_FIND_CLICKED, {"x": x, "y": y})
                    if selector:
                        try:
                            if len(self.recording.steps) >= MAX_STEPS:
                                raise WorkbenchError("录制步骤已达上限")
                            step = await recorded_click(page, selector, self.recording.origin)
                            self.recording.steps.append(step)
                        except (BrowserError, WorkbenchError):
                            self.recording.invalid = True
                await page.mouse.down()
            elif kind == "up" and "left" in self.buttons:
                self.buttons.discard("left")
                await page.mouse.up()
        elif kind == "wheel":
            delta = event.get("delta")
            if type(delta) in (int, float) and -2000 <= delta <= 2000:
                await page.mouse.wheel(0, delta)
        elif kind == "text":
            if self.recording:
                self.recording = None
            value = event.get("text")
            if isinstance(value, str) and 0 < len(value) <= 1024:
                await page.keyboard.insert_text(value)
            return {"recording": self.recording is not None}
        elif kind == "key":
            key = event.get("key")
            if key in {
                "Tab",
                "Shift+Tab",
                "Enter",
                "Escape",
                "Backspace",
                "Delete",
                "Space",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
                "Home",
                "End",
                "PageUp",
                "PageDown",
                "Control+a",
            }:
                await page.keyboard.press(key)

    async def close(self):
        self.recording = self.draft = self.captured = None
        self.frame_cache = self.frame_page = self.verification_probe = None
        self.buttons.clear()
        if self.expires:
            self.expires.cancel()
            self.expires = None
        if self.context_manager is not None:
            cm, self.context_manager, self.context, self.page = self.context_manager, None, None, None
            with suppress(Exception):
                await cm.__aexit__(None, None, None)

    async def _frame_verification(self, page):
        from playwright.async_api import Error as BrowserError

        now = time.monotonic()
        url = page.url
        probe = self.verification_probe
        same_page = probe is not None and probe[0] is page and probe[1] == url
        needs_verification = probe[3] if same_page else False
        if not same_page or now - probe[2] >= 1:
            # Bound the probe because screenshots and input share the session lock.
            with suppress(BrowserError, asyncio.TimeoutError):
                needs_verification = await asyncio.wait_for(challenge_visible(page), timeout=0.2)
            self.verification_probe = (page, url, now, needs_verification)
        return needs_verification

    async def capture_frame(self):
        page = self.active_page()
        now = time.monotonic()
        if self.frame_page is page and self.frame_cache and (now - self.last_frame_at) < 0.12:
            frame = self.frame_cache
        else:
            frame = await page.screenshot(type="jpeg", quality=65, timeout=1800)
            self.frame_cache = frame
            self.frame_page = page
            self.last_frame_at = now
        verification = await self._frame_verification(page)
        site = page_label(page.url)
        pages = self.context.pages if self.context else [page]
        active_idx = pages.index(page) if page in pages else 0
        return {
            "frame": frame,
            "site": site if "://" in site else "",
            "recording": "1" if self.recording else "0",
            "verification": "1" if verification else "0",
            "tabs": json.dumps([page_label(tab.url) for tab in pages], ensure_ascii=True),
            "active_tab": str(active_idx),
        }


async def storage_for(context, origin):
    from libs.security import domain_matches

    host = urlsplit(origin).hostname
    state = await context.storage_state(indexed_db=True)
    # Some OAuth providers leave long-lived credentials in their own storage.
    filtered = {
        "cookies": [cookie for cookie in state["cookies"] if domain_matches(cookie["domain"], host)],
        "origins": [entry for entry in state["origins"] if entry["origin"] == origin],
    }
    if len(json.dumps(filtered).encode()) > MAX_SESSION_BYTES:
        raise WorkbenchError("此站点的登录状态超过工作台可保存的大小")
    return filtered


async def persistable_state(context, origin):
    state = await storage_for(context, origin)
    if not state["cookies"] and not state["origins"]:
        raise WorkbenchError("这个站点未留下可复用的浏览器登录状态。请确认已登录后重试")
    return state


async def settle(page):
    from playwright.async_api import TimeoutError as BrowserTimeout

    with suppress(BrowserTimeout):
        await page.wait_for_load_state("networkidle", timeout=4000)


async def pcbeta_daily(page, context):
    """Run the built-in daily task in an existing browser with its normal egress policy."""
    from playwright.async_api import Error as BrowserError

    async with _pcbeta_lock:
        tabs = set(context.pages)
        rejected = False

        async def restrict(route):
            request = route.request
            if page_label(request.url) != signin_pcbeta.ORIGIN and (
                request.is_navigation_request() or request.method not in ("GET", "HEAD", "OPTIONS")
            ):
                await route.abort()
                return
            await route.fallback()

        def on_response(response):
            nonlocal rejected
            if (
                response.request.resource_type == "document"
                and page_label(response.url) == signin_pcbeta.ORIGIN
                and response.status in (401, 403)
            ):
                rejected = True

        async def check_page():
            if page.is_closed() or page_label(page.url) != signin_pcbeta.ORIGIN or set(context.pages) != tabs:
                raise WorkbenchError("远景论坛页面或标签页发生变化，请核对当前任务状态")
            if rejected or await challenge_visible(page):
                raise VerificationNeeded("远景论坛要求登录或验证，请在浏览器里完成后再核对任务")

        await context.route("**/*", restrict)
        context.on("response", on_response)
        try:
            return await signin_pcbeta.run_daily_checkin(page, check_page)
        except signin_pcbeta.DailyTaskError as exc:
            raise WorkbenchError(str(exc)) from exc
        except BrowserError as exc:
            with suppress(BrowserError):
                await check_page()
            raise WorkbenchError("无法核实远景论坛每日打卡结果，请查看站点当前状态") from exc
        finally:
            context.remove_listener("response", on_response)
            await context.unroute("**/*", restrict)


async def replay(script, state):
    """Run the recorded browser interactions against the user's encrypted session."""
    from playwright.async_api import Error as BrowserError

    validate_script(script)
    origin, url, steps = script["origin"], script["url"], script["steps"]
    if not state:
        raise VerificationNeeded("登录会话已失效，请在 AI 签到工作台重新验证")
    await validate_start_url(url)
    async with _replay_slots, isolated_browser(storage_state=state) as context:
        page = await context.new_page()

        async def restrict_replay(route):
            req = route.request
            try:
                different_origin = web_origin(req.url) != origin
            except ValueError:
                different_origin = True
            if different_origin and (
                req.is_navigation_request() or req.method not in ("GET", "HEAD", "OPTIONS")
            ):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", restrict_replay)
        recording = Recording(origin, url)
        context.on("response", lambda response: _record_response(recording, response))

        async def verification(message):
            return PageVerification(message, await storage_for(context, origin))

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
            if (
                (response and response.status in (401, 403))
                or page_label(page.url) != origin
                or await challenge_visible(page)
            ):
                raise await verification("站点要求重新登录或验证")
            if script.get("recipe") == signin_pcbeta.RECIPE:
                try:
                    receipt = await pcbeta_daily(page, context)
                except VerificationNeeded as exc:
                    raise await verification(str(exc)) from exc
                return receipt, await storage_for(context, origin)
            before = await success_signal(page)
            if already_checked(before):
                return "站点页面显示今日已签到", await storage_for(context, origin)
            recording.summaries.clear()
            recording.needs_verification = False
            for step in steps:
                observed = await recorded_click(page, step["selector"], origin)
                if observed["tag"] != step["tag"] or observed["text"] != step["text"]:
                    raise WorkbenchError("签到按钮已经变化，请重新录制")
                await page.locator(step["selector"]).click(timeout=6500)
                await settle(page)
                if len(context.pages) != 1 or page_label(page.url) != origin or await challenge_visible(page):
                    raise await verification("站点在签到时要求人工验证")
            if recording.needs_verification:
                raise await verification("签到接口要求重新验证或登录")
            after = await success_signal(page)
            if after and after != before:
                return "站点页面确认签到成功", await storage_for(context, origin)
            raise WorkbenchError("网页未出现可确认的签到结果，请检查步骤或重新录制")
        except BrowserError as exc:
            if not page.is_closed():
                with suppress(BrowserError):
                    if await challenge_visible(page):
                        raise await verification("站点要求人工验证") from exc
            raise WorkbenchError("浏览器未能回放签到步骤，请在工作台检查网站页面") from exc


def already_checked(signal):
    return bool(re.search(r"已(?:经)?签到|已打卡|already", signal))
