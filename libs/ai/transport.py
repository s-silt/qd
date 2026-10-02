#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""OpenAI-compatible HTTP 传输。不含 HAR 语义，便于单测注入 session。"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from libs.ai.compatible import chat_completions_url, isolate_headers
from libs.ai.errors import AICanceled, AIClientError, map_http_error
from libs.ai.redact import redact_secrets


class ChatTransport:
    """一次 unary 或 stream attempt。取消只结束本次连接。"""

    def __init__(self, base_url: str, api_key: str, timeout: int):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    async def post_json(
        self,
        payload: Mapping[str, Any],
        *,
        session: Any = None,
    ) -> Dict[str, Any]:
        import aiohttp

        url = chat_completions_url(self.base_url)
        headers = isolate_headers(self.api_key)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        owns_session = session is None
        sess = session or aiohttp.ClientSession(timeout=timeout)
        try:
            async with sess.post(url, json=dict(payload), headers=headers) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise map_http_error(resp.status, text)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise AIClientError(
                        f"AI 响应非合法 JSON: {redact_secrets(text[:300])}"
                    ) from exc
        except aiohttp.ClientError as exc:
            raise AIClientError(
                f"连接 AI 服务失败: {redact_secrets(str(exc))}",
                retryable=True,
                kind="transport",
            ) from exc
        finally:
            if owns_session:
                await sess.close()
        if not isinstance(data, dict):
            raise AIClientError("AI 响应结构不符合预期: 顶层不是对象")
        return data

    async def stream(
        self,
        payload: Mapping[str, Any],
        *,
        session: Any = None,
    ) -> AsyncIterator[str]:
        """逐块产出 SSE 文本。调用方负责组帧与状态机。"""
        import aiohttp

        url = chat_completions_url(self.base_url)
        headers = isolate_headers(self.api_key)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        owns_session = session is None
        sess = session or aiohttp.ClientSession(timeout=timeout)
        try:
            async with sess.post(url, json=dict(payload), headers=headers) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise map_http_error(resp.status, text)
                async for chunk in resp.content.iter_any():
                    if not chunk:
                        continue
                    yield chunk.decode("utf-8", errors="replace")
        except aiohttp.ClientError as exc:
            raise AIClientError(
                f"连接 AI 服务失败: {redact_secrets(str(exc))}",
                retryable=True,
                kind="transport",
            ) from exc
        finally:
            if owns_session:
                await sess.close()


def parse_unary_content(data: Mapping[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIClientError("AI 响应结构不符合预期") from exc
    return content or ""


async def read_stream_text(chunks: AsyncIterator[str], canceled: Optional[Any] = None) -> str:
    """读完 SSE 文本。``canceled`` 为可调用对象且返回真时，停止本次 attempt。"""
    parts = []
    async for part in chunks:
        if callable(canceled) and canceled():
            raise AICanceled()
        parts.append(part)
    return "".join(parts)
