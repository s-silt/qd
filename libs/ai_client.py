#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
OpenAI 兼容协议的 AI 客户端，用于辅助生成 QD 签到模板。

支持任意兼容 /v1/chat/completions 的服务：OpenAI、DeepSeek、通义千问、
Moonshot、本地 Ollama (启用 OpenAI 模式) 等。
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import config

try:
    from libs.log import Log  # type: ignore

    logger_ai = Log("QD.AI").getlogger()
except Exception:  # pragma: no cover - tornado 缺失时退化为标准 logger
    logger_ai = logging.getLogger("QD.AI")


class AIClientError(Exception):
    """AI 调用失败时抛出。"""


class AIClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        self.api_key = api_key or config.ai_api_key
        self.base_url = (base_url or config.ai_base_url).rstrip("/")
        self.model = model or config.ai_model
        self.timeout = timeout or config.ai_timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def chat(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]] = None,
        temperature: float = 0.2,
    ) -> str:
        if not self.enabled:
            raise AIClientError("未配置 AI_API_KEY，无法使用 AI 功能")

        # 延迟导入，避免无网络调用的工具函数测试需要 aiohttp
        import aiohttp

        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        # Only expose status code; do NOT echo response body to
                        # callers — it may contain reflected API keys or internal
                        # error detail from the upstream provider.
                        logger_ai.debug(
                            "AI service error %s, body (first 200): %.200s",
                            resp.status,
                            text,
                        )
                        raise AIClientError(
                            f"AI 服务返回错误状态码 {resp.status}，请检查 AI_API_KEY 及服务配置"
                        )
                    data = json.loads(text)
        except aiohttp.ClientError as e:
            raise AIClientError(f"连接 AI 服务失败: {e}") from e
        except json.JSONDecodeError as e:
            raise AIClientError(f"AI 响应非合法 JSON: {e}") from e

        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            # Log detail at debug; surface only generic message to caller
            logger_ai.debug("AI 响应结构不符合预期: %.500s", str(data))
            raise AIClientError("AI 响应结构不符合预期，请检查所选模型是否兼容 OpenAI Chat Completions 协议") from e


# ---------- HAR 预处理 ---------- #

# 静态/埋点等噪声请求过滤
_NOISE_EXT = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".map",
)
_NOISE_HOST_KEYWORDS = (
    "google-analytics", "googletagmanager", "doubleclick", "hotjar",
    "sentry.io", "bugsnag", "logrocket", "fullstory", "mixpanel",
    "segment.io", "cdn.jsdelivr", "unpkg.com", "fonts.googleapis",
    "fonts.gstatic", "baidu.com/hm.js", "cnzz.com", "umeng.com",
)


def _is_noise(entry: Dict[str, Any]) -> bool:
    try:
        url = entry["request"]["url"]
    except (KeyError, TypeError):
        return True
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    if any(path.endswith(ext) for ext in _NOISE_EXT):
        return True
    host = (parsed.hostname or "").lower()
    full = f"{host}{parsed.path}".lower()
    if any(kw in full for kw in _NOISE_HOST_KEYWORDS):
        return True
    mime = (
        entry.get("response", {})
        .get("content", {})
        .get("mimeType", "")
        .lower()
    )
    if mime.startswith(("image/", "font/", "video/", "audio/", "text/css")):
        return True
    if "javascript" in mime and "json" not in mime:
        return True
    return False


def _slim_entry(
    entry: Dict[str, Any],
    body_truncate: int = 500,
    header_truncate: int = 200,
) -> Dict[str, Any]:
    """裁剪单个 HAR entry 仅保留 AI 需要的字段。

    Args:
        entry: HAR entry
        body_truncate: 单条 body / response 内容超过此字节数会截断
        header_truncate: 单个 header value 超过此字节数会截断
    """
    req = entry.get("request", {})
    resp = entry.get("response", {})
    headers = req.get("headers", []) or []
    cookies = req.get("cookies", []) or []
    # 只保留有信息量的请求头
    keep_headers = {
        "content-type", "accept", "x-requested-with", "referer",
        "origin", "authorization", "x-csrf-token",
    }
    slim_headers = [
        {"name": h.get("name", ""), "value": h.get("value", "")[:header_truncate]}
        for h in headers
        if h.get("name", "").lower() in keep_headers
    ]
    post_data = req.get("postData", {}) or {}
    body_text = post_data.get("text", "") or ""
    if len(body_text) > body_truncate:
        body_text = body_text[:body_truncate] + "...(truncated)"

    resp_content = (resp.get("content", {}) or {}).get("text", "") or ""
    # 响应预览稍紧一点 (token 比例更重)
    resp_truncate = max(50, body_truncate * 4 // 5)
    if len(resp_content) > resp_truncate:
        resp_content = resp_content[:resp_truncate] + "...(truncated)"

    return {
        "method": req.get("method", "GET"),
        "url": req.get("url", "")[:500],
        "headers": slim_headers,
        "cookieNames": [c.get("name", "") for c in cookies][:10],
        "body": body_text,
        "respStatus": resp.get("status", 0),
        "respMime": (resp.get("content", {}) or {}).get("mimeType", ""),
        "respPreview": resp_content,
    }


def preprocess_har(
    har_data: Dict[str, Any],
    max_entries: int,
    body_truncate: Optional[int] = None,
    header_truncate: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """过滤静态资源后裁剪 entries。返回供 LLM 分析的精简列表。

    body_truncate / header_truncate 缺省时读 config; 单测可显式传入。
    """
    if body_truncate is None:
        body_truncate = getattr(config, "ai_har_body_truncate_bytes", 500)
    if header_truncate is None:
        header_truncate = getattr(config, "ai_har_header_truncate_bytes", 200)
    entries = (har_data.get("log", {}) or {}).get("entries", []) or []
    filtered = [e for e in entries if not _is_noise(e)]
    # 优先保留 POST/PUT 等修改类请求（签到通常是 POST/GET）
    filtered.sort(
        key=lambda e: (
            0 if e.get("request", {}).get("method", "GET").upper() != "GET" else 1
        )
    )
    return [
        _slim_entry(e, body_truncate=body_truncate, header_truncate=header_truncate)
        for e in filtered[:max_entries]
    ]


# ---------- Prompt 构造 ---------- #

_SYSTEM_PROMPT = """你是一个 HTTP 抓包分析助手。用户会给你一个浏览器抓包的 HAR 精简数据，
你的任务是从中识别出"签到 / 打卡 / check-in"操作真正发起的关键请求（通常只有 1-3 条），
忽略静态资源、心跳、埋点、广告、首页拉取等。

只输出严格 JSON，不要 markdown 包裹，不要解释，结构如下：
{
  "sitename": "站点名称（猜测）",
  "siteurl": "站点首页 URL",
  "note": "操作说明，10-50 字",
  "entries": [
    {
      "method": "POST",
      "url": "签到接口完整 URL",
      "headers": [{"name": "Content-Type", "value": "application/json"}],
      "body": "可能的请求体",
      "reason": "为什么判断这是签到请求（5-30 字）"
    }
  ],
  "variables": ["需要用户提供的变量名，如 cookie / token"],
  "success_keyword": "响应中表示签到成功的关键字（如 已签到 / success）"
}

判断要点：
1. 优先选择 POST 且 path 含 sign / check / clock / daily / attend / punch 等关键词的请求
2. 同一接口出现多次时只保留一次
3. 不要把登录请求当作签到请求，除非确无单独签到接口
4. 如果实在找不到签到请求，entries 返回空数组并在 note 中说明
"""


def build_messages(
    slim_entries: List[Dict[str, Any]], hint: str = ""
) -> List[Dict[str, str]]:
    user_payload = {
        "hint": hint or "",
        "entries": slim_entries,
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请基于以下抓包数据识别签到请求。数据使用 JSON：\n```json\n"
                + json.dumps(user_payload, ensure_ascii=False)
                + "\n```"
            ),
        },
    ]


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)


class HARSizeLimitExceeded(AIClientError):
    """sidecar 返回 HAR 超过配置上限时抛出。"""

    def __init__(self, limit: int, received: int):
        super().__init__(
            f"HAR 大小 {received} 字节超过上限 {limit}，"
            "请减少抓包页面或调高 PLAYWRIGHT_MAX_HAR_BYTES"
        )
        self.limit = limit
        self.received = received


async def read_capped(stream_iter, max_bytes: int) -> bytes:
    """从 async iter (例如 aiohttp resp.content.iter_chunked) 读取最多 max_bytes 字节。

    Args:
        stream_iter: async iterator of bytes chunks
        max_bytes: 最大允许字节数, 超过抛 HARSizeLimitExceeded

    Returns:
        累积的 bytes
    """
    chunks: List[bytes] = []
    received = 0
    async for chunk in stream_iter:
        received += len(chunk)
        if received > max_bytes:
            raise HARSizeLimitExceeded(max_bytes, received)
        chunks.append(chunk)
    return b"".join(chunks)


def parse_ai_response(content: str) -> Dict[str, Any]:
    """容错解析 AI 输出 JSON。

    依次尝试:
    1. 直接 json.loads (LLM 已严格输出 JSON)
    2. 提取 ```json ... ``` 围栏内文本
    3. 兜底: 找首个 { 到末尾 } 的子串
    """
    if not content or not content.strip():
        raise AIClientError("AI 输出为空")
    raw = content.strip()

    # 1. 直接尝试
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. ```json ... ``` 围栏 (兼容 ```/```json/```JSON, 含前后任意空白)
    m = _FENCE_RE.search(raw)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass

    # 3. 兜底: 截首个 { 到末尾 } 区段
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError as e:
            raise AIClientError(
                f"AI 输出截取后仍非合法 JSON: {e}; 原文: {content[:300]}"
            ) from e
    raise AIClientError(f"AI 输出不含 JSON 对象; 原文: {content[:300]}")


def ai_result_to_har(result: Dict[str, Any]) -> Dict[str, Any]:
    """把 AI 输出转成 QD 编辑器可加载的 HAR 结构。"""
    har_entries: List[Dict[str, Any]] = []
    for item in result.get("entries", []) or []:
        method = (item.get("method") or "GET").upper()
        url = item.get("url") or ""
        headers = []
        for h in item.get("headers", []) or []:
            if not isinstance(h, dict):
                continue
            headers.append(
                {"name": str(h.get("name", "")), "value": str(h.get("value", ""))}
            )
        body = item.get("body") or ""
        post_data = (
            {
                "mimeType": next(
                    (
                        h["value"]
                        for h in headers
                        if h["name"].lower() == "content-type"
                    ),
                    "application/x-www-form-urlencoded",
                ),
                "text": body,
            }
            if body
            else None
        )
        entry: Dict[str, Any] = {
            "request": {
                "method": method,
                "url": url,
                "headers": headers,
                "cookies": [],
                "queryString": [],
                "httpVersion": "HTTP/1.1",
                "headersSize": -1,
                "bodySize": len(body) if body else 0,
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [],
                "cookies": [],
                "content": {"size": 0, "mimeType": "text/plain", "text": ""},
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": -1,
            },
            "startedDateTime": "",
            "time": 0,
            "cache": {},
            "timings": {"send": 0, "wait": 0, "receive": 0},
            "_ai_reason": item.get("reason", ""),
        }
        if post_data:
            entry["request"]["postData"] = post_data
        har_entries.append(entry)

    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "QD AI", "version": "1.0"},
            "pages": [],
            "entries": har_entries,
        }
    }
