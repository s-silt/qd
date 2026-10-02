#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""日志与错误文本脱敏。密钥只存在于请求头，不进仓库、不进日志。"""

import re

_SECRET_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"
    r"|(sk-[A-Za-z0-9_\-]{8,})"
    r"|((?:api[_-]?key|token|secret)\s*[=:]\s*)\S+"
)


def redact_secrets(text: str) -> str:
    """替换 Bearer / sk- / api_key= 形态，保留足够上下文定位故障。"""
    if not text:
        return text

    def _sub(match: re.Match) -> str:
        if match.group(1):
            return match.group(1) + "[REDACTED]"
        if match.group(2):
            return "sk-[REDACTED]"
        return (match.group(3) or "") + "[REDACTED]"

    return _SECRET_RE.sub(_sub, text)
