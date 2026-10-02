#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""Chat Completions 流事件的共享转换。

对齐 gpt-load 5ddc867 的手法：并行 tool delta 先拆成「每次一个增量」，
再喂给同一个状态机。content delta、usage、finish_reason 不在路径间复制。

移除条件：上游 SDK 能在单次 feed 中正确消费多个 tool delta，
并且同套回归（并行工具、未知 index、交错 arguments、缺 finish）通过后，
``split_parallel_tool_deltas`` 可删，状态机保留。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence


class StreamProtocolError(Exception):
    """流协议不完整或 tool index 无法归属。"""


class ChatToResponsesState:
    """把 chat.completion.chunk 累积成一次完整 assistant 回合。

    状态只属于当前 attempt。调用方不得把同一个 state 复用到下一次请求。
    """

    def __init__(self) -> None:
        self.content_parts: List[str] = []
        self.tool_calls: Dict[int, Dict[str, Any]] = {}
        self.finish_reason: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None
        self.model: Optional[str] = None
        self.saw_done = False
        self.canceled = False

    def snapshot(self) -> Dict[str, Any]:
        ordered = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        return {
            "content": "".join(self.content_parts),
            "tool_calls": ordered,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "model": self.model,
            "saw_done": self.saw_done,
            "canceled": self.canceled,
        }


def split_parallel_tool_deltas(chunk: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """锁定「每次只读一个 tool 增量」时，把并行 delta 拆开。

    - 文本增量与 usage/finish 只留在最后一片，避免重复累计。
    - 单 tool 或无 tool 的 chunk 原样返回（浅拷贝），不改调用方对象。
    """
    choices = chunk.get("choices") or []
    if len(choices) != 1 or not isinstance(choices[0], Mapping):
        return [dict(chunk)]
    delta = choices[0].get("delta") or {}
    if not isinstance(delta, Mapping):
        return [dict(chunk)]
    calls = delta.get("tool_calls") or []
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)) or len(calls) < 2:
        return [dict(chunk)]

    parts: List[Dict[str, Any]] = []
    last = len(calls) - 1
    for index, call in enumerate(calls):
        part = dict(chunk)
        choice = dict(choices[0])
        part_delta = dict(delta) if index == 0 else {}
        part_delta["tool_calls"] = [call]
        choice["delta"] = part_delta
        if index != last:
            choice.pop("finish_reason", None)
            part.pop("usage", None)
        part["choices"] = [choice]
        parts.append(part)
    return parts


def _tool_slot(state: ChatToResponsesState, index: int) -> Dict[str, Any]:
    slot = state.tool_calls.get(index)
    if slot is None:
        slot = {"index": index, "id": "", "name": "", "arguments": ""}
        state.tool_calls[index] = slot
    return slot


def feed_chunk(state: ChatToResponsesState, chunk: Mapping[str, Any]) -> None:
    """把已经拆分过的 chunk 喂进状态机。未知且无 id 的 index 拒绝。"""
    if chunk.get("model"):
        state.model = str(chunk["model"])
    if chunk.get("usage") is not None:
        state.usage = dict(chunk["usage"])

    choices = chunk.get("choices") or []
    if not choices:
        return
    choice = choices[0] or {}
    if choice.get("finish_reason"):
        state.finish_reason = str(choice["finish_reason"])
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content:
        state.content_parts.append(str(content))

    introduced = {
        int(call.get("index", 0))
        for call in (delta.get("tool_calls") or [])
        if isinstance(call, Mapping) and call.get("id")
    }
    for call in delta.get("tool_calls") or []:
        if not isinstance(call, Mapping):
            continue
        if "index" not in call:
            raise StreamProtocolError("Chat tool call has no index")
        index = int(call["index"])
        known = index in state.tool_calls and bool(state.tool_calls[index].get("id"))
        if not call.get("id") and index not in introduced and not known:
            raise StreamProtocolError("Chat tool call has no ID or known index")
        slot = _tool_slot(state, index)
        if call.get("id"):
            slot["id"] = str(call["id"])
        fn = call.get("function") or {}
        if isinstance(fn, Mapping):
            if fn.get("name"):
                slot["name"] = str(fn["name"])
            if fn.get("arguments"):
                slot["arguments"] += str(fn["arguments"])


def parse_sse_data(frame: str) -> Optional[Dict[str, Any]]:
    """解析一条 ``data:`` 负载。``[DONE]`` 返回 None。"""
    text = frame.strip()
    if not text or text.startswith(":"):
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if text == "[DONE]":
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StreamProtocolError(f"非法 SSE JSON: {text[:120]}") from exc
    if not isinstance(data, dict):
        raise StreamProtocolError("SSE data 不是对象")
    return data


def iter_sse_events(raw: str) -> List[Optional[Dict[str, Any]]]:
    """把 SSE 文本拆成事件。``None`` 表示 ``[DONE]``。"""
    events: List[Optional[Dict[str, Any]]] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if payload == "[DONE]":
            events.append(None)
        else:
            events.append(parse_sse_data("data: " + payload))
    return events


def consume_sse(raw: str, state: Optional[ChatToResponsesState] = None) -> ChatToResponsesState:
    """用共享状态机消费一整段 SSE。缺 finish_reason 时拒绝终态。"""
    state = state or ChatToResponsesState()
    for event in iter_sse_events(raw):
        if event is None:
            state.saw_done = True
            continue
        for part in split_parallel_tool_deltas(event):
            feed_chunk(state, part)
    if state.canceled:
        raise StreamProtocolError("Chat stream canceled before a finish reason")
    if not state.finish_reason:
        raise StreamProtocolError("Chat stream ended without a finish reason")
    state.tool_calls = {
        call["index"]: call for call in order_tool_completions(state.snapshot()["tool_calls"])
    }
    return state


def order_tool_completions(tool_calls: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """终态按 index 输出，避免并行完成事件乱序。"""
    return [dict(call) for call in sorted(tool_calls, key=lambda c: int(c.get("index", 0)))]
