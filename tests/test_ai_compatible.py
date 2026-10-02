# -*- coding: utf-8 -*-
"""OpenAI-compatible 层回归：unary / stream / lifecycle / tool_output。

对照 gpt-load 5ddc867：共享转换、请求隔离、并行 tool delta、缺 finish 拒绝。
"""
import copy
import json

import pytest

from libs.ai.chat_conversion import (
    ChatToResponsesState,
    StreamProtocolError,
    consume_sse,
    feed_chunk,
    split_parallel_tool_deltas,
)
from libs.ai.compatible import (
    prepare_chat_request,
    prepare_tool_output_message,
    preserve_tool_turn_reasoning,
)
from libs.ai.errors import AIClientError, map_http_error
from libs.ai.redact import redact_secrets
from libs.ai.transport import parse_unary_content


def _sse(*payloads, done=True) -> str:
    lines = []
    for item in payloads:
        lines.append("data: " + json.dumps(item, ensure_ascii=False) + "\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _chunk(delta, finish=None, usage=None, model="m"):
    choice = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    body = {"id": "c", "model": model, "choices": [choice]}
    if usage is not None:
        body["usage"] = usage
    return body


class TestParallelToolSplit:
    def test_parallel_deltas_keep_content_and_terminal_on_last_part(self):
        chunk = _chunk(
            {
                "content": "checking",
                "tool_calls": [
                    {"index": 0, "id": "call_a", "function": {"name": "weather", "arguments": ""}},
                    {"index": 1, "id": "call_b", "function": {"name": "time", "arguments": ""}},
                ],
            },
            finish="tool_calls",
            usage={"total_tokens": 3},
        )
        parts = split_parallel_tool_deltas(chunk)
        assert len(parts) == 2
        assert parts[0]["choices"][0]["delta"]["content"] == "checking"
        assert "finish_reason" not in parts[0]["choices"][0]
        assert "usage" not in parts[0]
        assert parts[1]["choices"][0]["finish_reason"] == "tool_calls"
        assert parts[1]["usage"]["total_tokens"] == 3
        assert chunk["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_a"

    def test_single_tool_is_not_split(self):
        chunk = _chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "weather"}}]})
        parts = split_parallel_tool_deltas(chunk)
        assert len(parts) == 1
        assert parts[0] is not chunk

    def test_state_machine_keeps_both_parallel_calls(self):
        chunk = _chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "call_a", "function": {"name": "weather", "arguments": "{"}},
                    {"index": 1, "id": "call_b", "function": {"name": "time", "arguments": "{"}},
                ]
            }
        )
        state = ChatToResponsesState()
        for part in split_parallel_tool_deltas(chunk):
            feed_chunk(state, part)
        names = [c["name"] for c in state.snapshot()["tool_calls"]]
        assert names == ["weather", "time"]


class TestStreamLifecycle:
    def test_interleaved_arguments_and_unknown_index(self):
        raw = _sse(
            _chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "weather", "arguments": '{"ci'}}]}),
            _chunk({"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "time", "arguments": '{"tz'}}]}),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ty":"hz"}'}}]}),
            _chunk({"tool_calls": [{"index": 1, "function": {"arguments": '":"utc"}'}}]}, finish="tool_calls"),
            _chunk({}, usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}),
        )
        state = consume_sse(raw)
        calls = state.snapshot()["tool_calls"]
        assert calls[0]["arguments"] == '{"city":"hz"}'
        assert calls[1]["arguments"] == '{"tz":"utc"}'
        assert state.finish_reason == "tool_calls"
        assert state.usage["total_tokens"] == 3

    def test_unknown_index_without_id_rejected(self):
        raw = _sse(
            _chunk({"tool_calls": [{"index": 2, "function": {"arguments": "{"}}]}, finish="tool_calls"),
        )
        with pytest.raises(StreamProtocolError, match="known index"):
            consume_sse(raw)

    def test_missing_finish_rejected_with_or_without_done(self):
        partial = _chunk({"content": "partial"})
        for done in (False, True):
            with pytest.raises(StreamProtocolError, match="finish reason"):
                consume_sse(_sse(partial, done=done))

    def test_cancel_does_not_emit_completed_content_as_success(self):
        state = ChatToResponsesState()
        feed_chunk(state, _chunk({"content": "first token"}))
        state.canceled = True
        raw = _sse(_chunk({"content": "first token"}))
        with pytest.raises(StreamProtocolError, match="canceled"):
            consume_sse(raw, state)
        assert state.snapshot()["content"].startswith("first token")
        assert state.finish_reason is None

    def test_usage_after_finish_is_kept(self):
        raw = _sse(
            _chunk({"content": "done"}, finish="stop"),
            _chunk({}, usage={"total_tokens": 9}),
        )
        state = consume_sse(raw)
        assert state.finish_reason == "stop"
        assert state.usage["total_tokens"] == 9
        assert state.snapshot()["content"] == "done"


class TestRequestIsolation:
    def test_prepare_does_not_mutate_shared_history(self):
        history = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "reasoning_details": [{"type": "summary", "summary": "Need tools."}],
                "tool_calls": [{"id": "call_1", "function": {"name": "weather", "arguments": "{}"}}],
            },
        ]
        before = copy.deepcopy(history)
        payload = prepare_chat_request(
            history, model="deepseek-chat", base_url="https://api.deepseek.com/v1"
        )
        assert history == before
        assert payload["messages"][1]["reasoning_content"] == "Need tools."
        assert "reasoning_content" not in history[1]

    def test_encrypted_summary_not_promoted(self):
        messages = [
            {
                "role": "assistant",
                "reasoning_details": [
                    {"type": "encrypted", "data": "opaque"},
                    {"type": "summary", "summary": "Short."},
                ],
                "tool_calls": [{"id": "call_1"}],
            }
        ]
        before = copy.deepcopy(messages)
        preserve_tool_turn_reasoning(messages)
        assert "reasoning_content" not in messages[0]
        assert messages == before

    def test_user_boundary_blocks_old_reasoning(self):
        messages = [
            {"role": "assistant", "reasoning_content": "Old."},
            {"role": "user", "content": "New question."},
            {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
        ]
        preserve_tool_turn_reasoning(messages)
        assert "reasoning_content" not in messages[2]


class TestToolOutput:
    def test_tool_output_is_new_message(self):
        history = [{"role": "assistant", "tool_calls": [{"id": "call_1"}]}]
        before = copy.deepcopy(history)
        extra = prepare_tool_output_message("call_1", "done", name="weather")
        history.append(extra)
        assert history[0] == before[0]
        assert extra["role"] == "tool"
        assert extra["content"] == "done"
        assert extra["tool_call_id"] == "call_1"

    def test_appended_tool_output_does_not_rewrite_prior_turn(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "weather"}}]},
        ]
        history.append(prepare_tool_output_message("call_1", "sunny"))
        payload = prepare_chat_request(history, model="gpt-4o-mini", base_url="https://api.openai.com/v1")
        assert payload["messages"][-1]["content"] == "sunny"
        assert history[1].get("reasoning_content") is None


class TestUnaryAndErrors:
    def test_unary_content(self):
        data = {"choices": [{"message": {"content": '{"sitename":"x"}'}}]}
        assert "sitename" in parse_unary_content(data)

    def test_bad_shape(self):
        with pytest.raises(AIClientError):
            parse_unary_content({"choices": []})

    @pytest.mark.parametrize(
        "status,kind,retryable",
        [
            (400, "bad_request", False),
            (401, "auth", False),
            (429, "rate_limit", True),
            (503, "upstream", True),
        ],
    )
    def test_http_mapping(self, status, kind, retryable):
        err = map_http_error(status, 'rejected sk-supersecretkey')
        assert err.status == status
        assert err.kind == kind
        assert err.retryable is retryable
        assert "supersecretkey" not in str(err)

    def test_redact_bearer(self):
        text = redact_secrets("Authorization Bearer sk-abcdefghijklmnopqrstuvwxyz failed")
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in text
        assert "[REDACTED]" in text


class TestIsolationExtras:
    def test_prepare_messages_are_deep_copies(self):
        history = [{"role": "user", "content": "hi"}]
        payload = prepare_chat_request(
            history, model="gpt-4o-mini", base_url="https://api.openai.com/v1"
        )
        assert payload["messages"][0] is not history[0]
        assert payload["messages"] is not history

    def test_tool_result_boundary_blocks_old_reasoning(self):
        messages = [
            {"role": "assistant", "reasoning_content": "Old."},
            {"role": "assistant", "tool_calls": [{"id": "old"}]},
            {"role": "tool", "tool_call_id": "old", "content": "done"},
            {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
        ]
        preserve_tool_turn_reasoning(messages)
        assert "reasoning_content" not in messages[3]


class TestCancelTransport:
    @staticmethod
    def _run(coro):
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            # 留给同进程里仍用 get_event_loop() 的旧用例，避免关掉当前 loop。
            pass

    def test_read_stream_text_cancel_raises_ai_canceled(self):
        from libs.ai.errors import AICanceled
        from libs.ai.transport import read_stream_text

        async def gen():
            yield "data: {\"choices\":[]}\n\n"
            yield "data: [DONE]\n\n"

        async def run():
            with pytest.raises(AICanceled):
                await read_stream_text(gen(), canceled=lambda: True)

        self._run(run())

    def test_chat_stream_missing_finish_rejects(self):
        from libs.ai.client import AIClient
        from libs.ai.chat_conversion import StreamProtocolError

        class FakeTransport:
            async def stream(self, payload, session=None):
                yield 'data: {"id":"c","choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
                yield "data: [DONE]\n\n"

        client = AIClient(api_key="sk-test", base_url="http://example", transport=FakeTransport())

        async def run():
            with pytest.raises(StreamProtocolError, match="finish reason"):
                await client.chat_stream([{"role": "user", "content": "hi"}])

        self._run(run())
