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
                        raise AIClientError(
                            f"AI 服务返回 {resp.status}: {text[:500]}"
                        )
                    data = json.loads(text)
        except aiohttp.ClientError as e:
            raise AIClientError(f"连接 AI 服务失败: {e}") from e
        except json.JSONDecodeError as e:
            raise AIClientError(f"AI 响应非合法 JSON: {e}") from e

        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise AIClientError(f"AI 响应结构不符合预期: {data}") from e


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

_SYSTEM_PROMPT = """你是一个高级 QD 签到模板生成助手。你的任务是从 HAR 抓包数据中识别签到/任务流程，生成完整的 QD 模板。

## QD 模板格式

模板是 JSON 数组，每个元素是一个请求步骤：

```json
{
  "comment": "步骤说明（可选）",
  "request": {
    "method": "GET/POST",
    "url": "请求URL，支持 {{变量名}}",
    "headers": [{"name": "Header-Name", "value": "header-value"}],
    "cookies": [],
    "data": "POST数据（POST请求时）",
    "mimeType": "application/json"
  },
  "rule": {
    "success_asserts": [{"re": "200|成功关键字", "from": "status|content"}],
    "failed_asserts": [{"re": "失败关键字", "from": "content"}],
    "extract_variables": [{"name": "变量名", "re": "正则(含捕获组)", "from": "content|header"}]
  }
}
```

## 变量系统

- `{{cookie}}` - 用户提供的 Cookie
- `{{token}}` / `{{csrf}}` - 从响应中提取的 token
- `__log__` - 特殊变量，QD 会读取并显示为任务日志

## 识别规则（重要！）

### 1. 单步签到
直接 POST 签到接口，响应含成功消息。

### 2. 多步签到（必须保留完整流程！）
常见模式：
- **获取 token → 签到**：先 GET 页面提取 csrf/token，再 POST 签到
- **获取签到信息 → 执行签到 → 查询结果**：三步流程
- **多任务签到**：多个独立的签到接口

### 3. 复杂场景
- **JSON 响应**：用 `"re": "\"key\":\"(.*?)\""` 提取
- **HTML 响应**：用 `"re": "<span>(.*?)</span>"` 提取
- **带时间戳的请求**：保留原始 URL，QD 会自动处理
- **需要 Referer 的请求**：必须保留关键 header

### 4. 变量提取
从响应中提取变量供后续步骤使用：
```json
{"name": "csrf_token", "re": "\"csrf_token\":\"(.*?)\"", "from": "content"}
```

## 日志输出（必须包含！）

最后一步必须输出 `__log__`，格式示例：
```
用户名：xxx 签到时间：xxx 获得积分：xxx 总积分：xxx
```

### 方式一：直接提取
```json
{"name": "__log__", "re": "\"msg\":\"(.+?)\"", "from": "content"}
```

### 方式二：拼接变量（推荐）
```json
{
  "request": {
    "method": "POST",
    "url": "api://util/unicode",
    "headers": [],
    "cookies": [],
    "data": "content=签到结果：{{msg}} 获得积分：{{points}}"
  },
  "rule": {
    "success_asserts": [{"re": "200", "from": "status"}],
    "extract_variables": [
      {"name": "__log__", "re": "\"转换后\": \"(.*)\"", "from": "content"}
    ]
  }
}
```

## 辅助 API
- `api://util/unicode`: 转义内容，data=`content=文本`，响应 `"转换后": "结果"`
- `api://util/string/replace`: 字符串替换

## 输出格式

只输出 JSON，不要 markdown：
```json
{
  "sitename": "站点名称",
  "siteurl": "站点URL",
  "note": "操作说明",
  "har": [...],
  "variables": ["cookie", "其他需要的变量"]
}
```

## 完整示例

### 示例1：简单签到
输入：POST /api/checkin → {"msg":"签到成功","points":10}

输出：
```json
{
  "sitename": "示例站",
  "siteurl": "https://example.com",
  "note": "每日签到",
  "har": [
    {
      "request": {
        "method": "POST",
        "url": "https://example.com/api/checkin",
        "headers": [
          {"name": "Content-Type", "value": "application/json"},
          {"name": "Cookie", "value": "{{cookie}}"}
        ],
        "cookies": [],
        "data": "{}",
        "mimeType": "application/json"
      },
      "rule": {
        "success_asserts": [{"re": "200", "from": "status"}],
        "failed_asserts": [],
        "extract_variables": [
          {"name": "msg", "re": "\"msg\":\"(.*?)\"", "from": "content"},
          {"name": "points", "re": "\"points\":(\\d+)", "from": "content"}
        ]
      }
    },
    {
      "comment": "输出日志",
      "request": {
        "method": "POST",
        "url": "api://util/unicode",
        "headers": [],
        "cookies": [],
        "data": "content={{msg}} 获得积分：{{points}}"
      },
      "rule": {
        "success_asserts": [{"re": "200", "from": "status"}],
        "extract_variables": [
          {"name": "__log__", "re": "\"转换后\": \"(.*)\"", "from": "content"}
        ]
      }
    }
  ],
  "variables": ["cookie"]
}
```

### 示例2：获取 token + 签到
输入：
- GET /page → HTML 含 csrf_token="abc123"
- POST /api/checkin (带 token) → {"success":true}

输出：
```json
{
  "har": [
    {
      "comment": "获取 csrf token",
      "request": {
        "method": "GET",
        "url": "https://example.com/page",
        "headers": [{"name": "Cookie", "value": "{{cookie}}"}],
        "cookies": []
      },
      "rule": {
        "success_asserts": [{"re": "200", "from": "status"}],
        "extract_variables": [
          {"name": "csrf_token", "re": "csrf_token=\"(.*?)\"", "from": "content"}
        ]
      }
    },
    {
      "comment": "执行签到",
      "request": {
        "method": "POST",
        "url": "https://example.com/api/checkin",
        "headers": [
          {"name": "Content-Type", "value": "application/json"},
          {"name": "Cookie", "value": "{{cookie}}"},
          {"name": "X-CSRF-Token", "value": "{{csrf_token}}"}
        ],
        "cookies": [],
        "data": "{\"token\":\"{{csrf_token}}\"}",
        "mimeType": "application/json"
      },
      "rule": {
        "success_asserts": [{"re": "success", "from": "content"}],
        "extract_variables": []
      }
    }
  ],
  "variables": ["cookie"]
}
```

### 示例3：多任务签到
输入：
- POST /api/sign_in → 签到
- POST /api/lottery → 抽奖
- GET /api/user_info → 用户信息

输出：保留所有任务接口，每个都执行。

## 关键原则

1. **保留完整流程**：如果看到 获取token→签到 的模式，必须保留两步
2. **保留必要 header**：Cookie、Content-Type、X-CSRF-Token 等必须保留
3. **变量提取要准确**：正则必须有捕获组 `()`
4. **必须有日志**：最后一步必须输出 `__log__`
5. **错误处理**：添加 `failed_asserts` 识别重复签到等情况
6. **用户变量**：用 `{{变量名}}` 标记需要用户提供的值
"""


def build_messages(
    slim_entries: List[Dict[str, Any]], hint: str = ""
) -> List[Dict[str, str]]:
    user_payload = {
        "hint": hint or "",
        "entries": slim_entries,
    }

    user_content = """请分析以下 HAR 抓包数据，识别签到/任务流程并生成 QD 模板。

要求：
1. 识别完整的签到流程（可能多步）
2. 保留必要的请求 header（Cookie、CSRF Token 等）
3. 从响应中提取变量（积分、天数、状态等）
4. 最后一步必须输出 __log__ 日志
5. 如果有获取 token 的步骤，必须保留

数据：
```json
""" + json.dumps(user_payload, ensure_ascii=False) + """
```"""

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
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
    """把 AI 输出转成 QD 编辑器可加载的 HAR 结构。

    支持两种格式：
    1. 新格式：AI 直接输出 QD 模板数组在 result["har"] 中
    2. 旧格式：AI 输出 result["entries"]，需要转换
    """
    # 新格式：AI 直接输出 QD 模板
    if "har" in result and isinstance(result["har"], list):
        return result["har"]

    # 旧格式兼容：转换 entries 为 QD 模板
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

        # 构建 QD 格式的请求
        request: Dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers,
            "cookies": [],
        }
        if body:
            request["data"] = body
            # 从 headers 中获取 Content-Type
            content_type = next(
                (h["value"] for h in headers if h["name"].lower() == "content-type"),
                "application/x-www-form-urlencoded",
            )
            request["mimeType"] = content_type

        # 构建规则
        success_keyword = result.get("success_keyword", "")
        rule: Dict[str, Any] = {
            "success_asserts": [{"re": "200", "from": "status"}],
            "failed_asserts": [],
            "extract_variables": [],
        }
        # 如果有成功关键字，添加到断言
        if success_keyword:
            rule["success_asserts"].append(
                {"re": success_keyword, "from": "content"}
            )

        entry: Dict[str, Any] = {
            "request": request,
            "rule": rule,
        }
        # 添加步骤说明
        if item.get("reason"):
            entry["comment"] = item["reason"]

        har_entries.append(entry)

    # 如果有成功关键字，添加一个日志输出步骤
    if result.get("success_keyword"):
        har_entries.append({
            "comment": "输出签到结果",
            "request": {
                "method": "POST",
                "url": "api://util/unicode",
                "headers": [],
                "cookies": [],
                "data": "html_unescape=false&content=签到成功！",
            },
            "rule": {
                "success_asserts": [{"re": "200", "from": "status"}],
                "failed_asserts": [],
                "extract_variables": [
                    {"name": "__log__", "re": "\"转换后\": \"(.*)\"", "from": "content"}
                ],
            },
        })

    return har_entries
