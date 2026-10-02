#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""QD 使用的 OpenAI-compatible 客户端。

主路径是 unary chat，用来生成签到模板。stream / tool 回合走同一套
prepare + chat_conversion，避免再复制一份状态机。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

import config

from libs.ai.chat_conversion import ChatToResponsesState, consume_sse
from libs.ai.compatible import prepare_chat_request, prepare_tool_output_message
from libs.ai.errors import AIClientError
from libs.ai.transport import ChatTransport, parse_unary_content, read_stream_text

try:
    from libs.log import Log  # type: ignore

    logger_ai = Log("QD.AI").getlogger()
except Exception:  # pragma: no cover - tornado 缺失时退化为标准 logger
    logger_ai = logging.getLogger("QD.AI")


class AIClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        transport: Optional[ChatTransport] = None,
    ):
        self.api_key = api_key if api_key is not None else config.ai_api_key
        self.base_url = (base_url or config.ai_base_url).rstrip("/")
        self.model = model or config.ai_model
        self.timeout = timeout or config.ai_timeout
        self.transport = transport or ChatTransport(
            self.base_url, self.api_key, self.timeout
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def prepare(
        self,
        messages: List[Dict[str, Any]],
        *,
        response_format: Optional[Dict[str, str]] = None,
        temperature: float = 0.2,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise AIClientError("未配置 AI_API_KEY，无法使用 AI 功能")
        return prepare_chat_request(
            messages,
            model=self.model,
            base_url=self.base_url,
            temperature=temperature,
            response_format=response_format,
            stream=stream,
            tools=tools,
        )

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, str]] = None,
        temperature: float = 0.2,
        *,
        session: Any = None,
    ) -> str:
        payload = self.prepare(
            messages, response_format=response_format, temperature=temperature
        )
        data = await self.transport.post_json(payload, session=session)
        return parse_unary_content(data)

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.2,
        tools: Optional[List[Dict[str, Any]]] = None,
        session: Any = None,
        canceled: Any = None,
    ) -> ChatToResponsesState:
        """消费 SSE 并返回终态。缺 finish_reason 或中途取消时拒绝终态。"""
        payload = self.prepare(
            messages, temperature=temperature, stream=True, tools=tools
        )
        raw = await read_stream_text(
            self.transport.stream(payload, session=session), canceled=canceled
        )
        state = ChatToResponsesState()
        return consume_sse(raw, state)

    @staticmethod
    def tool_output(call_id: str, output: str, name: str = "") -> Dict[str, str]:
        return prepare_tool_output_message(call_id, output, name=name)

    def log_failure(self, exc: BaseException) -> None:
        from libs.ai.redact import redact_secrets

        logger_ai.warning("AI 调用失败: %s", redact_secrets(str(exc)))
