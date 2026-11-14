"""Human-operated browser sessions and bounded, credential-free capture summaries."""

import asyncio
import re
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from libs import playwright_capture
from libs.browser_egress import BrowserEgress, public_addresses
from libs.button_finder import JS_FIND_CANDIDATES

VIEWPORT = {"width": 1100, "height": 740}
SESSION_SECONDS = 600
MAX_RECORDS = 60
MAX_STEPS = 6
MAX_SESSION_BYTES = 2 * 1024 * 1024
AUTH_FIELD = re.compile(
    r"(?i)pass(?:word|wd)?|cookie|authorization|secret|credential|session|token|csrf|"
    r"otp|captcha|sms|(?:^|[_-])(?:pin|code|phone|mobile|email|account|username|key)(?:$|[_-])"
)
AUTH_PATH = re.compile(
    r"(?i)(?:^|[/_.-])(?:login|log[-_]?in|logout|sign[-_]in|auth|oauth\w*|sso|passport|"
    r"captcha|verify|verification|challenge|sms|otp|qrconnect)(?:$|[/_.-])"
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
SUCCESS = re.compile(
    r"签到成功|(?<!未)(?:已签到|已经签到)|重复签到|打卡成功|(?<!未)已打卡|checked\s*in|already\s+checked",
    re.I,
)
CLICK_CANDIDATE = re.compile(
    r"^(?:立即|每日|今日|去|点击)?(?:签到|打卡)(?:领取奖励)?$|^(?:daily )?check[ -]?in$", re.I
)
GOTO_TIMEOUT = 15000
_replay_slots = asyncio.Semaphore(max(1, playwright_capture.PLAYWRIGHT_MAX_CONCURRENT))
_browser_lock = asyncio.Lock()
_browser = None
_driver = None


class WorkbenchError(Exception):
    pass


class VerificationNeeded(WorkbenchError):
    pass


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
    if not can_record(origin, start_url):
        raise WorkbenchError("请登录后打开站点内不带查询参数的签到页面，再开始录制")
    if not steps or len(steps) > MAX_STEPS:
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


def can_record(origin, url):
    parsed = urlsplit(url)
    actual = web_origin(url)
    same = origin == actual or origin.startswith("http://") and actual == "https://" + origin[7:]
    return same and not parsed.query and not AUTH_PATH.search(parsed.path)


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
    text = await page.locator("body").inner_text(timeout=2500)
    return bool(CHALLENGE.search(text[:2200]))


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
        self.lock = asyncio.Lock()
        self.expires = None
        self.context_manager = None
        self.context = None
        self.page = None
        self.recording = None
        self.captured = None
        self.draft = None
        self.saved = None
        self.buttons = set()

    async def open(self, state=None):
        self.context_manager = isolated_browser(storage_state=state)
        self.context = await self.context_manager.__aenter__()
        self.page = await self.context.new_page()
        self.context.on("page", self._new_tab)
        self.context.on("response", self._on_response)
        await self.goto(self.start_url)

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
        origin = web_origin(start_url)
        if not can_record(web_origin(self.start_url), start_url):
            raise WorkbenchError("请登录后打开原站点内不带查询参数的签到页面，再开始录制")
        self.recording = Recording(origin, start_url, initial_success=await success_signal(page))
        self.draft = self.captured = None
        self.saved = None

    async def auto_click(self):
        from playwright.async_api import Error as BrowserError

        page = self.active_page()
        candidates = await page.evaluate(JS_FIND_CANDIDATES)
        matches = [item for item in candidates if CLICK_CANDIDATE.fullmatch(item["text"].strip())]
        if len(matches) != 1:
            return False
        selector = matches[0]["selector"]
        locator = page.locator(selector)
        if await locator.count() != 1 or not await locator.is_enabled():
            return False
        try:
            step = await recorded_click(page, selector, self.recording.origin)
            await locator.click(timeout=3500)
        except (BrowserError, WorkbenchError):
            return False
        self.recording.steps.append(step)
        return True

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
        if self.expires:
            self.expires.cancel()
        if self.context is not None:
            self.context = None
            await self.context_manager.__aexit__(None, None, None)


async def storage_for(context, origin):
    import json

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


async def replay(script, state):
    """Run the recorded browser interactions against the user's encrypted session."""
    from playwright.async_api import Error as BrowserError

    origin, url, steps = script["origin"], script["url"], script["steps"]
    validate_steps(origin, url, steps)
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
