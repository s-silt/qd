# 从 gpt-load 5ddc867 迁到 QD 的做法

对照提交：<https://github.com/tbphp/gpt-load/commit/5ddc867f2e43dd109b1b08b5a3e7c05c4a7539c8>

QD 仍是 HTTP 定时任务 / HAR 签到框架。这次只借用该提交的分层和测试手法，没有引入 gpt-load 的渠道、计费或 Responses 网关。

## 学到什么

1. 流转换只留一份。gpt-load 把 `clineChatStreamEvents` 收成 `chatToResponsesStreamEvents`，cline 与 compatible 共用。并行 tool delta 先拆成「每次一个增量」，再喂同一状态机；文本、usage、finish_reason 只留在最后一片。
2. 兼容层和主执行路径分开。`prepareCompatibleChatRequest` 只改本次 attempt 的副本，共享历史不被就地修改。临时补丁写明移除条件。
3. 流的终态有时序。finish 之后的 usage 要保留；没有 finish_reason 不能当成成功结束；未知且无 id 的 tool index 直接拒绝。
4. 回归按行为切：unary、stream、lifecycle、tool_output。覆盖并行工具、交错 arguments、取消、缺 finish。

## 改了什么

| gpt-load | QD |
| --- | --- |
| `chat_conversion.go` | `libs/ai/chat_conversion.py`：`split_parallel_tool_deltas` + `ChatToResponsesState` |
| `compatible.go` prepare | `libs/ai/compatible.py`：`prepare_chat_request`，DeepSeek/Moonshot tool 回合补 `reasoning_content` |
| `compatible_stream.go` | `AIClient.chat_stream` 走同一状态机；缺 finish 抛 `StreamProtocolError` |
| transport 与业务拆开 | `libs/ai/transport.py` 只做 HTTP / 错误映射；HAR 在 `libs/ai/har_pipeline.py` |
| 请求隔离 | prepare 深拷贝 messages；tool 结果是新消息，不回写 assistant |
| 回归测试 | `tests/test_ai_compatible.py` |
| 敏感日志 | `libs/ai/redact.py`，分析失败日志与返回文案脱敏 |

`libs/ai_client.py` 保留为垫片，旧测试的 `from libs.ai_client import ...` 仍可用。

HAR 分析仍是四步，handler 只串联返回值：`preprocess_har` → `build_messages` → `AIClient.chat` → `parse_ai_response` / `ai_result_to_har`。校验用 `validate_ai_template`，只产生告警，不改模板。`/har/ai_analyze` 多返回 `warnings`。`analyze_har` / `apply_ai_result` 是同一流水线的纯函数封装，给不经过 HTTP 的调用方用。

Worker 的 `do()` 仍返回 `True` / `False`，避免打断用身份比较判断成败的调用方。同一次 attempt 的终态写在 `worker.last_result`（`TaskRunResult`，含 `reason`）。退避、夜间窗口、瞬时错误判定仍是 `BaseWorker` 上的纯函数，`tests/test_worker_schedule.py` 直接测源码，不再复制一份退避表。

AI key 只读 `AI_API_KEY`。空则功能关闭。启动日志只打 model 与 base_url。

## 刻意没改什么

- 不实现 gpt-load 的多协议网关、Responses API、namespace tools、Anthropic/Gemini 转换。
- 不改签到执行器 `libs/fetcher.py` 的请求语义，不改模板 JSON 格式。
- 不把 HAR 编辑器改成聊天产品。AI 只辅助生成签到模板。
- 不重写 `QueueWorker` / `BatchWorker` 的调度循环，只把 attempt 终态显式化。
- 不提交 `.env`、`local_config.py`、密钥或本机绝对路径。
- DeepSeek/Moonshot 的 reasoning 回填是临时兼容。网关自己补齐后，连同 `preserve_tool_turn_reasoning` 一起删，回归保留。

## 迁移注意

- 新代码从 `libs.ai` 导入。`libs.ai_client` 仍导出原符号。
- `/har/ai_analyze` 成功响应增加 `warnings` 数组。旧前端忽略未知字段即可。
- `worker.BaseWorker.do` 仍返回布尔。失败原因在 `worker.last_result.reason`。
- 环境变量名称未变：`AI_API_KEY`、`AI_BASE_URL`、`AI_MODEL`、`AI_TIMEOUT` 及 HAR 截断相关项。
