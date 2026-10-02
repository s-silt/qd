#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""QD AI 辅助签到：OpenAI 兼容客户端与 HAR 模板生成。

公开符号保持与历史 ``libs.ai_client`` 一致，供 handler 与测试继续导入。
"""

from libs.ai.client import AIClient
from libs.ai.errors import AIClientError, HARSizeLimitExceeded
from libs.ai.har_pipeline import (
    ai_result_to_har,
    analyze_har,
    apply_ai_result,
    build_messages,
    find_overbroad_asserts,
    parse_ai_response,
    preprocess_har,
    read_capped,
    validate_ai_template,
)
from libs.ai.redact import redact_secrets

__all__ = [
    "AIClient",
    "AIClientError",
    "HARSizeLimitExceeded",
    "ai_result_to_har",
    "analyze_har",
    "apply_ai_result",
    "build_messages",
    "find_overbroad_asserts",
    "parse_ai_response",
    "preprocess_har",
    "read_capped",
    "redact_secrets",
    "validate_ai_template",
]
