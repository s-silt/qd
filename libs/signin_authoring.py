"""User-authorized HAR sign-in template authoring with credential-free AI selection."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from libs.ai_client import AIClient, AIClientError, parse_ai_response
from libs.signin_workbench import (
    AUTH_FIELD,
    AUTH_PATH,
    CHALLENGE,
    CLOUDFLARE_VERIFICATION,
    SENSITIVE_VALUE,
    WorkbenchError,
    capture_summary,
    is_public_signin_query,
    web_origin,
)

MAX_HAR_BYTES = 2 * 1024 * 1024
MAX_HAR_ENTRIES = 400
MAX_CANDIDATES = 24
MAX_SELECTED_STEPS = 6
MAX_HEADERS = 64
MAX_QUERY_FIELDS = 24
MAX_BODY_FIELDS = 24
MAX_BODY_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_JSON_DEPTH = 5
MAX_JSON_KEYS = 64
MAX_OUTCOMES_PER_CANDIDATE = 6

_STATIC_EXT = (
    ".css",
    ".js",
    ".mjs",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".mp3",
    ".map",
    ".pdf",
    ".zip",
)

_EXTRA_AUTH_PATH = re.compile(
    r"(?i)(?:^|[/_.-])(?:register|sign[-_]?up|forgot|reset|password|passwd|session|"
    r"token|callback|authorize|saml|oidc|mfa|2fa|totp|turnstile|recaptcha|hcaptcha|"
    r"geetest|cf-chl|cdn-cgi|challenge-platform)(?:$|[/_.-])"
)

_CHALLENGE_HEADER = re.compile(
    r"(?i)^(?:cf-mitigated|cf-chl-bypass|x-vercel-mitigated|x-waf-event)$"
)

_PROTECTION_BODY = re.compile(
    r"(?i)cf-browser-verification|cf_chl_opt|challenge-platform|turnstile|"
    r"g-recaptcha|h-captcha|geetest|attention required!?\s*\|\s*cloudflare|"
    r"<title>\s*(?:just a moment|attention required|安全验证|访问验证|人机验证)|"
    r"<input[^>]+type=[\"']?password\b"
)

_NEGATED_SUCCESS = re.compile(
    r"(?i)(?:尚未|还未|未|请先|点击|立即|去)(?:签到|打卡)|"
    r"not\s+(?:yet\s+)?checked\s*in|haven'?t\s+checked\s*in|"
    r"签到失败|打卡失败|check[-_\s]?in\s+failed"
)

_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-\u4e00-\u9fff]{1,64}$")
_PARAM_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_BUSINESS_FIELDS = frozenset({
    "activity_id", "attendance_date", "attendance_id", "campaign_id", "checkin_date",
    "event_id", "period_id", "round_id", "schedule_id",
})
_BUSINESS_VALUE = r"[A-Za-z0-9_-]{1,40}"
_JSON_KEY = r'"[A-Za-z0-9_.-]{1,40}"'
_JSON_STRING = r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
_JSON_SCALAR = rf'(?:{_JSON_STRING}|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)'
_CSRF_HEADER_NAMES = {
    "x-csrf-token": "X-CSRF-Token",
    "x-xsrf-token": "X-XSRF-Token",
    "csrf-token": "CSRF-Token",
    "xsrf-token": "XSRF-Token",
    "x-csrftoken": "X-CSRFToken",
}

# Small allowlist of JSON scalar keys and values for literal assertions.
_BOOL_INT_SUCCESS_ALLOWLIST: Dict[str, Tuple[Any, ...]] = {
    "code": (0, 200),
    "status": (0, 1, 200, True),
    "ret": (0, 200),
    "retcode": (0, 200),
    "errcode": (0,),
    "errno": (0,),
    "ok": (True, 1),
    "success": (True, 1),
    "checked_in": (True, 1),
    "signed": (True, 1),
    "is_signed": (True, 1),
    "signed_in": (True, 1),
}

_SIGNIN_SPECIFIC_BOOL_KEYS = frozenset({"checked_in", "signed", "is_signed", "signed_in"})

_MESSAGE_KEYS = (
    "msg",
    "message",
    "errmsg",
    "info",
    "desc",
    "detail",
    "result",
    "status_text",
)

_KNOWN_SUCCESS_PHRASES: Tuple[str, ...] = (
    "今日已签到",
    "已经签到",
    "签到成功",
    "重复签到",
    "已签到",
    "今日已打卡",
    "打卡成功",
    "已打卡",
    "already checked in",
    "check-in successful",
    "checkin successful",
    "checked in",
)

_EXPLICIT_SIGNIN_SEGMENT = re.compile(
    r"(?i)(?:^|[/_.-])(?:check[-_]?in|attendance|qiandao|daily[-_]?sign|"
    r"signin|sign|punch|签到|打卡)(?:$|[/_.-])"
)

_SYSTEM_PROMPT = (
    "你是 QD 签到模板选择器。你只能根据给定的候选请求摘要，选择已观测到的候选 id "
    "以及预先计算的响应条件索引 condition_indices。\n"
    "严格禁止编写 URL、请求体、请求头、正则、脚本或任何未出现在候选列表中的字段。\n"
    "优先选择显式的签到 POST 或 GET 请求，保持录制顺序。"
    "business_dependencies 中的 source_id 是对应 target_id 的准备请求，"
    "由 QD 自动串接，无需为准备请求选择签到成功条件。\n"
    '只输出严格 JSON：{"steps": [{"id": 整数ID, "condition_indices": [整数索引]}]}'
)


@dataclass(frozen=True)
class PrecomputedCondition:
    index: int
    key: str
    value: Any
    regex: str
    signin_specific: bool = False


@dataclass
class _DraftCandidate:
    id: int
    order: int
    method: str
    path: str
    masked_path: str
    resource_type: str
    status: int
    signin_hint: bool
    has_signin_evidence: bool
    has_cookie: bool
    has_auth: bool
    request_template: Dict[str, Any]
    conditions: List[PrecomputedCondition] = field(default_factory=list)
    score: int = 0
    response_fields: Dict[str, str] = field(default_factory=dict, repr=False)
    input_fields: Dict[Tuple[str, str], str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class _BusinessBinding:
    source: int
    target: int
    slot: str
    key: str

    @property
    def variable(self) -> str:
        return f"qd_link_{self.source}_{self.key}"


@dataclass
class _ScanSummary:
    same_origin: int = 0
    ordinary_responses: int = 0
    supported_requests: int = 0
    missing_response: int = 0

    def failure_message(self, challenge_seen: int) -> str:
        if challenge_seen:
            return "抓包中检测到登录或安全验证页面。请先在浏览器完成验证，再录制签到。"
        if not self.same_origin:
            return "HAR 中没有与填写网址同源的请求。请填写实际签到页地址，重新导出该页面的签到记录。"
        if not self.ordinary_responses:
            return "HAR 中没有成功返回的同源签到请求。请登录后录制签到动作，等站点显示结果再导出。"
        if not self.supported_requests:
            return "同源请求的参数或格式暂不支持生成模板。可改用工作台的在线录制。"
        if self.missing_response:
            return "同源请求没有明确的签到特征，且部分响应缺少可读取的正文。请等签到结果出现后再导出脱敏 HAR，或改用在线录制。"
        return "找到同源请求，但未识别到签到动作或签到回执。请从点击签到开始录制，等站点显示结果后再导出。"


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sanitize_placeholder_name(prefix: str, key: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", key).strip("_").lower()
    if not cleaned:
        cleaned = "value"
    return f"{prefix}_{cleaned[:32]}"


def _validate_ordinary_path(raw_path: str) -> Optional[str]:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    if len(raw_path) > 240 or "\\" in raw_path or "%" in raw_path and re.search(r"%[0-9a-fA-F]{2}", raw_path):
        decoded = unquote(raw_path)
    else:
        decoded = raw_path
    if (
        not decoded.startswith("/")
        or len(decoded) > 240
        or "//" in decoded
        or any(ch in decoded for ch in "{}<>\\;\"'`$")
        or any(ord(ch) <= 32 or ord(ch) == 127 for ch in decoded)
    ):
        return None
    if AUTH_PATH.search(decoded) or _EXTRA_AUTH_PATH.search(decoded):
        return None
    segments = decoded.split("/")[1:]
    if len(segments) > 10:
        return None
    for idx, seg in enumerate(segments):
        if seg == "" and idx == len(segments) - 1:
            continue
        if seg in ("", ".", "..") or not _PATH_SEGMENT_RE.fullmatch(seg):
            return None
        if SENSITIVE_VALUE.search(seg):
            return None
    return decoded


def _parse_har_input(har: Any) -> List[Dict[str, Any]]:
    if isinstance(har, (bytes, bytearray)):
        if len(har) > MAX_HAR_BYTES:
            raise WorkbenchError("HAR 文件过大，请仅导出签到相关抓包")
        try:
            har = har.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkbenchError("HAR 文件编码无效，请使用 UTF-8 JSON") from exc
    if isinstance(har, str):
        if len(har.encode("utf-8")) > MAX_HAR_BYTES:
            raise WorkbenchError("HAR 内容过大，请仅导出签到相关抓包")
        try:
            har = json.loads(har)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise WorkbenchError("HAR JSON 格式无效") from exc

    if isinstance(har, dict):
        if isinstance(har.get("log"), dict) and isinstance(har["log"].get("entries"), list):
            entries = har["log"]["entries"]
        elif isinstance(har.get("entries"), list):
            entries = har["entries"]
        else:
            raise WorkbenchError("HAR 缺少 log.entries 列表")
    elif isinstance(har, list):
        entries = har
    else:
        raise WorkbenchError("HAR 数据结构无效")

    if not entries:
        raise WorkbenchError("HAR 中没有任何请求记录")
    if len(entries) > MAX_HAR_ENTRIES:
        raise WorkbenchError(f"HAR 请求条数超过上限（最多 {MAX_HAR_ENTRIES} 条），请裁剪后再试")
    if not all(isinstance(item, dict) for item in entries):
        raise WorkbenchError("HAR 条目格式无效")
    return entries


def _check_json_depth(obj: Any, depth: int = 0) -> bool:
    if depth > MAX_JSON_DEPTH:
        return False
    if isinstance(obj, dict):
        if len(obj) > MAX_JSON_KEYS:
            return False
        return all(
            isinstance(k, str) and len(k) <= 64 and _check_json_depth(v, depth + 1)
            for k, v in obj.items()
        )
    if isinstance(obj, list):
        if len(obj) > MAX_JSON_KEYS:
            return False
        return all(_check_json_depth(v, depth + 1) for v in obj)
    return isinstance(obj, (str, int, float, bool)) or obj is None


def _is_challenge_or_protection(
    status: int,
    resp_headers: Sequence[Tuple[str, str]],
    resp_text: str,
) -> bool:
    if status in (401, 403, 429, 503):
        if CHALLENGE.search(resp_text) or CLOUDFLARE_VERIFICATION.search(resp_text) or _PROTECTION_BODY.search(resp_text):
            return True
    for name, val in resp_headers:
        if _CHALLENGE_HEADER.match(name):
            return True
        if name.lower() == "server" and "cloudflare" in val.lower() and _PROTECTION_BODY.search(resp_text):
            return True
    sample = resp_text[:4096]
    if CHALLENGE.search(sample) or CLOUDFLARE_VERIFICATION.search(sample) or _PROTECTION_BODY.search(sample):
        return True
    return False


def _match_known_phrase(text: str) -> Optional[str]:
    if not text or len(text) > 240 or _NEGATED_SUCCESS.search(text):
        return None
    lower = text.lower()
    for phrase in _KNOWN_SUCCESS_PHRASES:
        if phrase in lower:
            return phrase
    return None


def _extract_precomputed_conditions(resp_text: str, mime_type: str) -> List[PrecomputedCondition]:
    conditions: List[PrecomputedCondition] = []
    if not resp_text or len(resp_text) > MAX_RESPONSE_BYTES:
        return conditions

    stripped = resp_text.strip()
    if stripped.startswith("{") and ("json" in mime_type.lower() or stripped.endswith("}")):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError):
            parsed = None
        if isinstance(parsed, dict) and _check_json_depth(parsed):
            containers: List[Dict[str, Any]] = [parsed]
            if isinstance(parsed.get("data"), dict):
                containers.append(parsed["data"])
            seen: set[Tuple[str, Any]] = set()
            for obj in containers:
                for key, allowed_vals in _BOOL_INT_SUCCESS_ALLOWLIST.items():
                    if key not in obj:
                        continue
                    val = obj[key]
                    for allowed in allowed_vals:
                        if type(val) is type(allowed) and val == allowed:
                            sig = (key, val)
                            if sig not in seen and len(conditions) < MAX_OUTCOMES_PER_CANDIDATE:
                                seen.add(sig)
                                literal = json.dumps(val)
                                regex = rf'"{re.escape(key)}"\s*:\s*{re.escape(literal)}(?=\s*[,}}])'
                                conditions.append(
                                    PrecomputedCondition(
                                        index=len(conditions),
                                        key=key,
                                        value=val,
                                        regex=regex,
                                        signin_specific=key in _SIGNIN_SPECIFIC_BOOL_KEYS,
                                    )
                                )
                for mkey in _MESSAGE_KEYS:
                    raw_val = obj.get(mkey)
                    if not isinstance(raw_val, str):
                        continue
                    phrase = _match_known_phrase(raw_val.strip())
                    if phrase is not None:
                        sig = (mkey, phrase)
                        if sig not in seen and len(conditions) < MAX_OUTCOMES_PER_CANDIDATE:
                            seen.add(sig)
                            regex = rf'(?i)"{re.escape(mkey)}"\s*:\s*"[^"]*{re.escape(phrase)}'
                            conditions.append(
                                PrecomputedCondition(
                                    index=len(conditions),
                                    key=mkey,
                                    value=phrase,
                                    regex=regex,
                                    signin_specific=True,
                                )
                            )
            return conditions

    phrase = _match_known_phrase(stripped[:240])
    if phrase is not None:
        conditions.append(
            PrecomputedCondition(
                index=0,
                key="text",
                value=phrase,
                regex="(?i)" + re.escape(phrase),
                signin_specific=True,
            )
        )
    return conditions


def _add_business_slots(
    observed: Dict[Tuple[str, str], str], slot: str, pairs: Sequence[Tuple[str, Any]]
) -> None:
    names = [_sanitize_placeholder_name(slot, key) for key, _ in pairs]
    for (key, value), name in zip(pairs, names):
        if (
            key in _BUSINESS_FIELDS and names.count(name) == 1
            and isinstance(value, str) and re.fullmatch(_BUSINESS_VALUE, value)
        ):
            observed[(slot, key)] = value


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON key")
    return obj


def _business_guard(key: str) -> str:
    # Match exactly one named field in a flat object, with the value in group 1.
    field = json.dumps(key)
    other = rf'(?!{field}\s*:){_JSON_KEY}\s*:\s*{_JSON_SCALAR}'
    return (
        rf'\A(?=[\s\S]{{1,{MAX_RESPONSE_BYTES}}}\Z)\s*\{{\s*(?:{other}\s*,\s*){{0,{MAX_JSON_KEYS - 1}}}'
        rf'{field}\s*:\s*"({_BUSINESS_VALUE})"\s*(?:,\s*{other}\s*){{0,{MAX_JSON_KEYS - 1}}}\}}\s*\Z'
    )


def _business_response(text: str, mime_type: str) -> Dict[str, str]:
    if not text or len(text) > MAX_RESPONSE_BYTES or "json" not in mime_type.lower():
        return {}
    try:
        obj = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(obj, dict) or len(obj) > MAX_JSON_KEYS:
        return {}
    if any(AUTH_FIELD.search(key) or _EXTRA_AUTH_PATH.search(key) or _PROTECTION_BODY.search(key) for key in obj):
        return {}
    result = {}
    for key in sorted(_BUSINESS_FIELDS & obj.keys()):
        match = re.fullmatch(_business_guard(key), text)
        if match and match[1] == obj[key]:
            result[key] = match[1]
    return result


def _build_safe_query_url(
    origin: str, path: str, query_str: str, observed: Dict[Tuple[str, str], str]
) -> Optional[str]:
    if not query_str:
        return f"{origin}{path}"
    try:
        pairs = parse_qsl(query_str, keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS)
    except ValueError:
        return None
    if len(pairs) > MAX_QUERY_FIELDS:
        return None
    placeholder_parts: List[str] = []
    for key, _ in pairs:
        if not _PARAM_KEY_RE.fullmatch(key) or AUTH_FIELD.search(key):
            return None
        ph = _sanitize_placeholder_name("query", key)
        placeholder_parts.append(key + "={{" + ph + "|urlencode}}")
    _add_business_slots(observed, "query", pairs)
    return f"{origin}{path}?{'&'.join(placeholder_parts)}" if placeholder_parts else f"{origin}{path}"


def _build_safe_body(
    post_data: Any, observed: Dict[Tuple[str, str], str]
) -> Tuple[Optional[str], Optional[str], bool]:
    """Return (data_template, mime_type, eligible)."""
    if not post_data:
        return None, None, True
    if not isinstance(post_data, dict):
        return None, None, False
    mime_raw = str(post_data.get("mimeType") or "").split(";")[0].strip().lower()
    text = post_data.get("text")
    params = post_data.get("params")

    if isinstance(text, str) and len(text.encode("utf-8")) > MAX_BODY_BYTES:
        return None, None, False

    if (not text or not str(text).strip()) and not params:
        return None, None, True

    if mime_raw == "application/json" or (isinstance(text, str) and text.strip().startswith("{")):
        if not isinstance(text, str):
            return None, None, False
        try:
            body_obj = json.loads(text.strip(), object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            return None, None, False
        if not isinstance(body_obj, dict) or len(body_obj) > MAX_BODY_FIELDS:
            return None, None, False
        fields: List[str] = []
        for key, val in body_obj.items():
            if (
                not isinstance(key, str)
                or not _PARAM_KEY_RE.fullmatch(key)
                or AUTH_FIELD.search(key)
                or _EXTRA_AUTH_PATH.search(key)
            ):
                return None, None, False
            if not isinstance(val, (str, int, float, bool)) and val is not None:
                return None, None, False
            ph = _sanitize_placeholder_name("body", key)
            fields.append(json.dumps(key) + ": {{" + ph + "|tojson}}")
        _add_business_slots(observed, "body", list(body_obj.items()))
        return "{" + ", ".join(fields) + "}", "application/json", True

    if mime_raw == "application/x-www-form-urlencoded" or isinstance(params, list):
        pairs: List[Tuple[str, str]] = []
        if isinstance(params, list) and params:
            if len(params) > MAX_BODY_FIELDS:
                return None, None, False
            for item in params:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    return None, None, False
                pairs.append((item["name"], str(item.get("value") or "")))
        elif isinstance(text, str):
            try:
                pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=MAX_BODY_FIELDS)
            except ValueError:
                return None, None, False
        else:
            return None, None, False

        if len(pairs) > MAX_BODY_FIELDS:
            return None, None, False
        encoded_parts: List[str] = []
        for key, _ in pairs:
            if not _PARAM_KEY_RE.fullmatch(key) or AUTH_FIELD.search(key) or _EXTRA_AUTH_PATH.search(key):
                return None, None, False
            ph = _sanitize_placeholder_name("form", key)
            encoded_parts.append(key + "={{" + ph + "|urlencode}}")
        _add_business_slots(observed, "form", pairs)
        return "&".join(encoded_parts), "application/x-www-form-urlencoded", True

    return None, None, False


def _extract_header_placeholders(
    req: Dict[str, Any], mime_type: Optional[str]
) -> Tuple[List[Dict[str, str]], bool, bool, bool]:
    """Return (headers, has_cookie, has_auth, ok)."""
    raw_headers = req.get("headers") or []
    raw_cookies = req.get("cookies") or []
    if not isinstance(raw_headers, list) or not isinstance(raw_cookies, list):
        return [], False, False, False
    if len(raw_headers) > MAX_HEADERS or len(raw_cookies) > MAX_HEADERS:
        return [], False, False, False

    has_cookie = bool(raw_cookies)
    has_auth = False
    csrf_header_name: Optional[str] = None

    for item in raw_headers:
        if not isinstance(item, dict):
            return [], False, False, False
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        lower = name.lower()
        if lower == "cookie":
            has_cookie = True
        elif lower == "authorization":
            has_auth = True
        elif lower in _CSRF_HEADER_NAMES and csrf_header_name is None:
            csrf_header_name = _CSRF_HEADER_NAMES[lower]
        elif "captcha" in lower or "turnstile" in lower or "geetest" in lower:
            return [], False, False, False

    headers: List[Dict[str, str]] = []
    if mime_type:
        headers.append({"name": "Content-Type", "value": mime_type})
    if has_cookie:
        headers.append({"name": "Cookie", "value": "{{cookie}}"})
    if has_auth:
        headers.append({"name": "Authorization", "value": "{{authorization}}"})
    if csrf_header_name:
        headers.append({"name": csrf_header_name, "value": "{{csrf_token}}"})
    return headers, has_cookie, has_auth, True


def _collect_candidates(
    entries: Sequence[Dict[str, Any]], origin: str, scan: Optional[_ScanSummary] = None
) -> Tuple[List[_DraftCandidate], int]:
    eligible: List[_DraftCandidate] = []
    challenge_seen = 0

    for order, entry in enumerate(entries):
        req = entry.get("request")
        resp = entry.get("response")
        if not isinstance(req, dict) or not isinstance(resp, dict):
            continue

        url = req.get("url")
        method = str(req.get("method") or "").upper()
        if not isinstance(url, str):
            continue

        try:
            entry_origin = web_origin(url)
        except ValueError:
            continue
        if entry_origin != origin:
            continue
        if scan is not None:
            scan.same_origin += 1
        if method not in ("GET", "POST"):
            continue

        parsed = urlsplit(url)
        if parsed.fragment:
            continue
        path = _validate_ordinary_path(parsed.path or "/")
        if path is None:
            continue

        lower_path = path.lower()
        content = resp.get("content") if isinstance(resp.get("content"), dict) else {}
        mime_type = str(content.get("mimeType") or "")
        if any(lower_path.endswith(ext) for ext in _STATIC_EXT) and "json" not in mime_type.lower():
            continue

        status = resp.get("status")
        if not _is_strict_int(status):
            continue

        raw_resp_headers = resp.get("headers") if isinstance(resp.get("headers"), list) else []
        resp_headers: List[Tuple[str, str]] = [
            (str(h.get("name") or ""), str(h.get("value") or ""))
            for h in raw_resp_headers[:MAX_HEADERS]
            if isinstance(h, dict)
        ]
        resp_text = content.get("text") if isinstance(content.get("text"), str) else ""
        if content.get("encoding") == "base64":
            resp_text = ""

        if _is_challenge_or_protection(status, resp_headers, resp_text):
            challenge_seen += 1
            continue

        if status < 200 or status >= 300:
            continue
        if scan is not None:
            scan.ordinary_responses += 1

        resource_type = str(entry.get("_resourceType") or "xhr").lower()
        if resource_type not in ("document", "fetch", "xhr"):
            resource_type = "xhr"

        try:
            summary = capture_summary(url, method, resource_type, status)
        except ValueError:
            continue
        if summary is None:
            continue

        observed: Dict[Tuple[str, str], str] = {}
        public_route = is_public_signin_query(parsed.path, parsed.query)
        if public_route:
            safe_url = f"{origin}{path}?{parsed.query}"
        else:
            safe_url = _build_safe_query_url(origin, path, parsed.query, observed)
        if safe_url is None:
            continue

        data_template, body_mime, body_ok = _build_safe_body(req.get("postData"), observed)
        if not body_ok:
            continue
        if method == "GET" and data_template is not None:
            continue

        headers, has_cookie, has_auth, headers_ok = _extract_header_placeholders(req, body_mime)
        if not headers_ok:
            continue
        if scan is not None:
            scan.supported_requests += 1
            if not resp_text.strip():
                scan.missing_response += 1

        conditions = _extract_precomputed_conditions(resp_text, mime_type)
        signin_hint = bool(summary["signin_hint"] or _EXPLICIT_SIGNIN_SEGMENT.search(path))
        has_explicit_outcome = any(c.signin_specific for c in conditions)
        # A public query can identify the plugin page as well as the actual action.
        # Require a live sign-in response before treating that route as an action.
        signin_hint = signin_hint or (public_route and has_explicit_outcome)
        has_signin_evidence = signin_hint or has_explicit_outcome

        # Require recognizable sign-in evidence for mutation candidates.
        if method == "POST" and not has_signin_evidence:
            continue

        request_template: Dict[str, Any] = {
            "method": method,
            "url": safe_url,
            "headers": headers,
            "cookies": [],
        }
        if data_template is not None and body_mime is not None:
            request_template["data"] = data_template
            request_template["mimeType"] = body_mime

        score = 0
        if method == "POST" and signin_hint:
            score += 100
        elif method == "POST" and has_explicit_outcome:
            score += 85
        elif method == "GET" and signin_hint:
            score += 70
        elif method == "GET" and has_explicit_outcome:
            score += 60
        elif conditions:
            score += 20

        eligible.append(
            _DraftCandidate(
                id=0,
                order=order,
                method=method,
                path=path,
                masked_path=str(summary["path"]),
                resource_type=str(summary["type"]),
                status=int(summary["status"]),
                signin_hint=signin_hint,
                has_signin_evidence=has_signin_evidence,
                has_cookie=has_cookie,
                has_auth=has_auth,
                request_template=request_template,
                conditions=conditions,
                score=score,
                response_fields=_business_response(resp_text, mime_type) if method == "GET" and not has_signin_evidence else {},
                input_fields=observed,
            )
        )

    if len(eligible) > MAX_CANDIDATES:
        ranked = sorted(eligible, key=lambda c: (c.score, -c.order), reverse=True)[:MAX_CANDIDATES]
        kept = {c.order for c in ranked}
        omitted_fields = {
            item for cand in eligible if cand.order not in kept for item in cand.response_fields.items()
        }
        for cand in ranked:
            cand.response_fields = {
                key: value for key, value in cand.response_fields.items() if (key, value) not in omitted_fields
            }
        eligible = sorted(ranked, key=lambda c: c.order)

    for idx, cand in enumerate(eligible, start=1):
        cand.id = idx
    return eligible, challenge_seen


def _find_bindings(candidates: Sequence[_DraftCandidate]) -> List[_BusinessBinding]:
    bindings = []
    sources: Dict[Tuple[str, str], List[int]] = {}
    for cand in candidates:
        if cand.has_signin_evidence:
            for (slot, key), value in cand.input_fields.items():
                possible = sources.get((key, value), [])
                if len(possible) == 1:
                    bindings.append(_BusinessBinding(possible[0], cand.id, slot, key))
        # Sources with their own unknown inputs aren't safe to pull in automatically.
        if cand.response_fields and "{{" not in cand.request_template["url"]:
            for key, value in cand.response_fields.items():
                sources.setdefault((key, value), []).append(cand.id)
    return bindings


def _build_model_messages(
    candidates: Sequence[_DraftCandidate], bindings: Sequence[_BusinessBinding]
) -> List[Dict[str, str]]:
    payload = {
        "candidates": [
            {
                "id": int(cand.id),
                "method": cand.method,
                "path": cand.masked_path,
                "type": cand.resource_type,
                "status": int(cand.status),
                "signin_hint": bool(cand.signin_hint),
                "outcomes": [
                    {
                        "index": int(cond.index),
                        "key": cond.key,
                        "value": cond.value,
                    }
                    for cond in cand.conditions
                ],
            }
            for cand in candidates
        ],
        "business_dependencies": [
            {"source_id": item.source, "target_id": item.target, "field": item.key}
            for item in bindings
        ],
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _validate_model_selection(
    raw_result: Any, by_id: Dict[int, _DraftCandidate]
) -> List[Tuple[_DraftCandidate, List[PrecomputedCondition]]]:
    if not isinstance(raw_result, dict):
        raise WorkbenchError("AI 返回结果格式无效")
    allowed_top_keys = {"steps"}
    if not set(raw_result.keys()) <= allowed_top_keys or "steps" not in raw_result:
        raise WorkbenchError("AI 返回了未授权的顶层字段")

    raw_steps = raw_result["steps"]
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > MAX_SELECTED_STEPS:
        raise WorkbenchError("AI 未选择有效的签到步骤（需 1 到 6 步）")

    seen_ids: set[int] = set()
    selections: List[Tuple[_DraftCandidate, List[PrecomputedCondition]]] = []

    for item in raw_steps:
        if not isinstance(item, dict):
            raise WorkbenchError("AI 步骤项格式无效")
        if not set(item.keys()) <= {"id", "condition_indices"} or "id" not in item:
            raise WorkbenchError("AI 步骤项包含未授权字段")
        cid = item["id"]
        if not _is_strict_int(cid) or cid not in by_id:
            raise WorkbenchError("AI 选择了不存在的候选请求")
        if cid in seen_ids:
            raise WorkbenchError(f"AI 重复选择了候选请求 ID: {cid}")
        seen_ids.add(cid)

        cand = by_id[cid]
        raw_indices = item.get("condition_indices", [])
        if raw_indices is None:
            raw_indices = []
        if not isinstance(raw_indices, list) or len(raw_indices) > len(cand.conditions):
            raise WorkbenchError("AI 返回的响应断言索引列表无效")

        chosen_conditions: List[PrecomputedCondition] = []
        seen_idx: set[int] = set()
        for cidx in raw_indices:
            if not _is_strict_int(cidx) or cidx < 0 or cidx >= len(cand.conditions) or cidx in seen_idx:
                raise WorkbenchError("AI 选择了无效的响应断言索引")
            seen_idx.add(cidx)
            chosen_conditions.append(cand.conditions[cidx])

        selections.append((cand, chosen_conditions))

    selections.sort(key=lambda pair: pair[0].order)
    if not any(cand.has_signin_evidence for cand, _ in selections):
        raise WorkbenchError("所选请求缺少明确的签到特征，无法生成签到草稿")
    return selections


async def author_template(har: Any, site: str, client: Optional[AIClient] = None) -> Dict[str, Any]:
    """Generate a credential-free QD sign-in draft template from a user-authorized HAR."""
    try:
        origin = web_origin(site)
    except ValueError as exc:
        raise WorkbenchError(str(exc)) from exc

    entries = _parse_har_input(har)
    scan = _ScanSummary()
    candidates, challenge_seen = await asyncio.to_thread(_collect_candidates, entries, origin, scan)

    if not candidates or not any(c.has_signin_evidence for c in candidates):
        raise WorkbenchError(scan.failure_message(challenge_seen))

    ai = client if client is not None else AIClient()
    if not ai.enabled:
        raise WorkbenchError("模型未配置，请联系 QD 管理员")
    bindings = _find_bindings(candidates)
    messages = _build_model_messages(candidates, bindings)
    try:
        raw_content = await ai.chat(messages, response_format={"type": "json_object"}, temperature=0.1)
        parsed_output = parse_ai_response(raw_content)
    except AIClientError as exc:
        raise WorkbenchError("模型未能返回有效的签到草稿，请重试") from exc

    by_id = {cand.id: cand for cand in candidates}
    selections = _validate_model_selection(parsed_output, by_id)

    selected_ids = {cand.id for cand, _ in selections}
    applied_bindings = [binding for binding in bindings if binding.target in selected_ids]
    missing_sources = {binding.source for binding in applied_bindings} - selected_ids
    if len(selected_ids) + len(missing_sources) > MAX_SELECTED_STEPS:
        raise WorkbenchError("签到请求和依赖的准备步骤超过 6 步，请缩短录制范围后再分析")
    selections.extend((by_id[source], []) for source in missing_sources)
    selections.sort(key=lambda pair: pair[0].order)
    selected_ids.update(missing_sources)
    step_number = {cand.id: idx for idx, (cand, _) in enumerate(selections, start=1)}
    steps: List[Dict[str, Any]] = []
    warnings: List[str] = []
    has_signin_assertion = any(cand.has_signin_evidence and conditions for cand, conditions in selections)
    receipt_step = next(
        cand.id for cand, _ in reversed(selections) if cand.has_signin_evidence
    )
    unverified_count = 0

    for idx, (cand, chosen_conditions) in enumerate(selections, start=1):
        request = dict(cand.request_template)
        success_asserts = [{"re": cond.regex, "from": "content"} for cond in chosen_conditions]
        extracted = {}
        for binding in applied_bindings:
            if cand.id == binding.source:
                extracted[binding.variable] = _business_guard(binding.key)
            if cand.id == binding.target:
                field_name = "url" if binding.slot == "query" else "data"
                old = "{{" + _sanitize_placeholder_name(binding.slot, binding.key) + "|"
                request[field_name] = request[field_name].replace(old, "{{" + binding.variable + "|")
        if extracted:
            guards = [rf"(?={pattern})" for pattern in extracted.values()]
            success_asserts = [
                {"re": "".join(guards), "from": "content"},
                {"re": r"^2\d\d$", "from": "status"},
            ]
        if not success_asserts:
            unverified_count += 1
            warnings.append(
                f"步骤 {idx}（{cand.method} {cand.path}）未匹配到受支持的成功断言字段，"
                "草稿未经验证，请在编辑器中手动补充成功断言"
            )
        if cand.has_signin_evidence and not success_asserts:
            # Fail closed in the legacy runner until the author adds a real condition.
            success_asserts = [{"re": "(?!)", "from": "content"}]
        steps.append(
            {
                "comment": f"签到步骤 {idx}: {cand.method} {cand.path}",
                "request": request,
                "rule": {
                    "success_asserts": success_asserts,
                    "failed_asserts": [{"re": "(?i)请(?:先|重新)登录|登录(?:已?失效|已?过期)|"
                                        "安全验证已失效|performing\\s+security\\s+verification|"
                                        "verify you are human|login required|签到失败|打卡失败",
                                        "from": "content"}],
                    "extract_variables": [
                        {"name": name, "re": pattern, "from": "content"} for name, pattern in extracted.items()
                    ],
                    **({"signin_receipt": True} if cand.id == receipt_step else {}),
                },
            }
        )

    if not has_signin_assertion:
        warnings.append("这份草稿缺少签到成功的响应证据。请在编辑器里补充成功条件后再试跑。")
    if not any(cand.has_cookie or cand.has_auth for cand, _ in selections):
        warnings.append("抓包中未包含 Cookie 或 Authorization 请求头，如需登录态请在编辑器中补充凭据变量")

    if challenge_seen:
        warnings.append(f"已自动跳过 {challenge_seen} 条验证或站点防护响应")

    if unverified_count:
        summary = (
            f"已为 {origin} 生成 {len(steps)} 个签到草稿步骤（含 {unverified_count} 个未验证步骤，"
            "请补充成功断言并配置本地凭据后测试）"
        )
    else:
        summary = f"已为 {origin} 生成 {len(steps)} 个签到草稿步骤，请在编辑器中填入本地凭据后测试"

    return {
        "steps": steps,
        "warnings": warnings,
        "summary": summary,
        "bindings": [
            {
                "source_step": step_number[binding.source],
                "target_step": step_number[binding.target],
                "field": binding.key,
                "location": binding.slot,
                "variable": binding.variable,
            }
            for binding in applied_bindings
        ],
        "candidates": [
            {
                "id": cand.id,
                "method": cand.method,
                "path": cand.path,
                "status": cand.status,
                "selected": cand.id in selected_ids,
            }
            for cand in candidates
        ],
        "site": origin,
    }
