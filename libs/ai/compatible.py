#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""OpenAI-compatible 请求准备。

只修正本次 attempt 新生成的 payload，不改调用方传入的 messages。
DeepSeek / Moonshot 的 tool 回合需要把同回合 reasoning 写回带 tool_calls 的消息；
有加密推理时不用摘要冒充全文。

移除条件：供应商网关自己补齐 tool-turn reasoning，且
``tests/test_ai_compatible.py`` 的隔离与边界用例仍绿。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Optional, Sequence

from libs.ai.errors import AIClientError
from urllib.parse import urlparse

_DEEPSEEK_MARKERS = ("deepseek",)
_MOONSHOT_MARKERS = ("moonshot", "kimi")


def provider_kind(base_url: str, model: str) -> str:
    host = (urlparse(base_url or "").hostname or "").lower()
    model_l = (model or "").lower()
    blob = f"{host} {model_l}"
    if any(m in blob for m in _DEEPSEEK_MARKERS):
        return "deepseek"
    if any(m in blob for m in _MOONSHOT_MARKERS):
        return "moonshot"
    if "dashscope" in host or model_l.startswith("qwen"):
        return "qwen"
    if "11434" in host or "ollama" in host:
        return "ollama"
    return "openai"


def _join_text(parts: Sequence[str]) -> str:
    return "\n".join(p for p in parts if p)


def preserve_tool_turn_reasoning(messages: List[Dict[str, Any]]) -> None:
    """#666 的 QD 对应物：只改本次 Chat 请求副本。

    完整推理优先于摘要；加密内容存在时不把摘要当成完整推理。
    用户消息或 tool 结果会切断回合，避免把上一轮推理写进新的 tool call。
    """
    start = 0
    while start < len(messages):
        if messages[start].get("role") != "assistant":
            start += 1
            continue
        end = start
        reasoning: List[str] = []
        summaries: List[str] = []
        has_encrypted = False
        while end < len(messages) and messages[end].get("role") == "assistant":
            msg = messages[end]
            end += 1
            if msg.get("reasoning_content"):
                reasoning.append(str(msg["reasoning_content"]))
            for detail in msg.get("reasoning_details") or []:
                if not isinstance(detail, Mapping):
                    continue
                kind = detail.get("type")
                if kind == "summary" and detail.get("summary"):
                    summaries.append(str(detail["summary"]))
                elif kind == "encrypted" and detail.get("data"):
                    has_encrypted = True
        if not reasoning and not has_encrypted:
            reasoning = summaries
        if reasoning:
            text = _join_text(reasoning)
            for index in range(start, end):
                assistant = messages[index]
                if assistant.get("tool_calls"):
                    assistant["reasoning_content"] = text
        start = end


def prepare_chat_request(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str,
    base_url: str = "",
    temperature: float = 0.2,
    response_format: Optional[Mapping[str, Any]] = None,
    stream: bool = False,
    tools: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """构造本次 attempt 的 chat payload。原 messages 不被就地修改。"""
    copied = copy.deepcopy(list(messages))
    if provider_kind(base_url, model) in ("deepseek", "moonshot"):
        preserve_tool_turn_reasoning(copied)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": copied,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = dict(response_format)
    if stream:
        payload["stream"] = True
    if tools:
        payload["tools"] = copy.deepcopy(list(tools))
    assert_request_isolated(messages, payload)
    return payload


def prepare_tool_output_message(
    call_id: str,
    output: str,
    *,
    name: str = "",
) -> Dict[str, str]:
    """tool 结果作为新消息追加，不回写历史里的 assistant 消息。"""
    msg = {"role": "tool", "tool_call_id": call_id, "content": output}
    if name:
        msg["name"] = name
    return msg


def chat_completions_url(base_url: str) -> str:
    root = (base_url or "").rstrip("/")
    if root.endswith("/chat/completions"):
        return root
    return f"{root}/chat/completions"


def isolate_headers(api_key: str, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(dict(extra))
    return headers


def assert_request_isolated(
    original: Sequence[Mapping[str, Any]], prepared: Mapping[str, Any]
) -> None:
    """准备步骤不得与共享历史共用对象。供调用方在调试时断言。"""
    for src, dst in zip(original, prepared.get("messages") or []):
        if src is dst:
            raise AIClientError("prepared message aliases shared history")
