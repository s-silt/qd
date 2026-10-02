#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""兼容垫片。实现已迁到 ``libs.ai``。

新代码请 ``from libs.ai import ...``。本模块只再导出 HAR/调度真正需要的公开 API；
``_is_noise`` / ``_extract_response_signals`` / ``_SYSTEM_PROMPT`` 等私有符号
请直接从 ``libs.ai.har_pipeline`` 导入，不再经本垫片 re-export。
"""

from libs.ai import (
    AIClient,
    AIClientError,
    HARSizeLimitExceeded,
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
    "validate_ai_template",
]
