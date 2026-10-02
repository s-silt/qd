#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""AI 兼容层错误。与签到执行错误分开，避免 HTTP 失败污染任务调度判定。"""

from typing import Any, Dict, Optional


class AIClientError(Exception):
    """AI 调用或模板解析失败。"""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        retryable: bool = False,
        kind: str = "ai_error",
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.kind = kind


class AICanceled(AIClientError):
    """调用方取消了本次 attempt，不回写共享历史。"""

    def __init__(self, message: str = "AI 请求已取消"):
        super().__init__(message, kind="canceled")


class HARSizeLimitExceeded(AIClientError):
    """HAR 字节数超过配置上限。"""

    def __init__(self, limit: int, received: int):
        super().__init__(
            f"HAR 大小 {received} 字节超过上限 {limit}，"
            "请减少抓包页面或调高上传上限"
        )
        self.limit = limit
        self.received = received


def map_http_error(status: int, body: str) -> AIClientError:
    """把上游 HTTP 状态映射成可测的兼容层错误，不把密钥原文留在消息里。"""
    from libs.ai.redact import redact_secrets

    clipped = redact_secrets((body or "")[:500])
    retryable = status == 429 or status >= 500
    if status == 401 or status == 403:
        kind = "auth"
    elif status == 429:
        kind = "rate_limit"
    elif status >= 500:
        kind = "upstream"
    elif status >= 400:
        kind = "bad_request"
    else:
        kind = "http"
    return AIClientError(
        f"AI 服务返回 {status}: {clipped}",
        status=status,
        retryable=retryable,
        kind=kind,
    )


def public_error_fields(exc: BaseException) -> Dict[str, Any]:
    """给 HTTP handler 的稳定字段。消息先脱敏，避免把密钥回给浏览器。"""
    from libs.ai.redact import redact_secrets

    return {
        "error": redact_secrets(str(exc)),
        "kind": getattr(exc, "kind", "ai_error"),
        "retryable": bool(getattr(exc, "retryable", False)),
        "status": getattr(exc, "status", None),
    }
