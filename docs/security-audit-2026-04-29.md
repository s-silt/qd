# QD 安全审计 2026-04-29

## 概要

| ���度 | 值 |
|------|----|
| 扫描日期 | 2026-04-29 |
| 扫描范围 | `libs/`, `config.py`, `run.py`, `db/`, `web/handlers/`, `web/app.py`, `web/fastapi/` (claude/fastapi-foundation 分支), `services/playwright/` |
| 总发现 | 14 项 |
| 🔴 严重 | 2 项 |
| 🟡 中等 | 6 项 |
| 🟢 低优/信息 | 6 项 |
| 已落地修复 | 4 项 (Phase 1) |
| 新增测试 | 28 个 |

---

## 严重问题（🔴）

### S-1 AIClient 错误响应体直接透传给调用方（已修复）

**文件**: `libs/ai_client.py:79-81`  
**修复状态**: ✅ 已修

**攻击场景**：  
`AIClient.chat()` 在 AI 服务返回 4xx 时，将原始响应体（`text[:500]`）包装进 `AIClientError` 然后一路冒泡到 `web/handlers/har.py`，最终通过 `await self.finish({"ok": False, "error": str(e)})` 输出给前端用户。部分 AI 提供商（OpenAI、Anthropic 等）会在 401/429 错误体中 echo 请求头摘要或 API key 前缀，攻击者可通过构造特定请求触发��误，读取返回的 JSON error ���段，进而获取 API key 信息。  

**修复内容**：  
- 将 4xx 原始响应体改为仅记录到 `logger_ai.debug()`，对调用方只暴露状态码（`"AI 服务返回错误状态码 401，请检查 AI_API_KEY 及服务配置"`）。  
- 同样���`AI 响应结构不符合预期` 时的完整 `data` 对象也不再透传，改为 debug 级日志。

---

### S-2 `HARAutoCaptureStatus` 匿名可访问且暴露内网 sidecar URL（未修，受 handler 限制）

**文件**: `web/handlers/har.py:747-756`  
**修复状态**: ⚠️ 未修（受限于"不动 handler 文件"约束）

**攻击场景**：  
`/har/auto_capture_status` 无需认证即可访问，响应中包含 `"sidecar_url": "http://playwright:8924"` 这一内部服务地址。攻击者无需登录即可枚举内网拓扑（容器名/端口/协议），为后续 SSRF 或内网探测提供基础信息。

**建议修复**：  
在 `HARAutoCaptureStatus.get()` 中添加 `@tornado.web.authenticated` 装饰器，或在响��中将 `sidecar_url` 替换为布尔值 `enabled` 即可（不暴露具体 URL）。  

```python
# 建议: 仅返回 enabled 状态，不暴露内网地址
await self.finish({"enabled": bool(config.playwright_sidecar_url)})
```

---

## 中等问题（🟡）

### M-1 默认密钥告警遗漏 MAIL_SMTP 无认证场景（已修复）

**文件**: `run.py:_check_default_secrets`  
**修复状态**: ✅ 已修

**问题**: 原检查仅覆盖 `COOKIE_SECRET` 和 `AES_KEY`。当 `MAIL_SMTP` 已配置但 `MAIL_PASSWORD` 为空时，邮件以无认证方式发出，这在支持 open relay 的 SMTP 服务器上可能导致邮件伪造（SPF/DKIM 问题）。  
**修复**: 新增警告 `"[安全] MAIL_SMTP 已配置但 MAIL_PASSWORD 为空"` 并排除已配置 `mailgun_key` 的情况（Mailgun 使用 API key 而非 SMTP 认证）。

---

### M-2 `cookie_secure_mode` 默认 False，cookie 在 HTTP 下明文传输

**文件**: `config.py:51-53`  
**修复状态**: ⚠️ 未修（改变默认值会破坏向后兼容性）

**问题**: `COOKIE_SECURE_MODE` 默认 `False`。生产��署���未显式��用 HTTPS + `cookie_secure_mode=True`，`user` 会话 cookie 在 HTTP 下以明文传输，可被中间人窃取。  
**建议**: 在启动日志增加对 HTTP 部署 + 无 `secure` 标志的提示，或在文档中明确要求生产环境开��。

---

### M-3 SSRF：`ALLOW_HOSTS` 默认为空，Playwright sidecar 接受任意 URL

**文件**: `services/playwright/app.py:56, 134-137`  
**修复状态**: 已有 WARNING（设计已知 limit）

**问题**: `ALLOW_HOSTS` 为��时，sidecar 会向任意 URL 发起带 Cookie 的 Chrome 请求，可被用于：
1. 访问云服务商 metadata 端点（`169.254.169.254`）
2. 访问内网 HTTP 服务并携带用户 cookie

代码已在 `lifespan` 里输出 WARNING，但 WARNING 不构成防护。  
**建议**: 生产文档中将 `ALLOW_HOSTS` 标记为**必须配置**，并考虑在 URL 验证���增加对 RFC 1918 / link-local 地址段的默��拒绝规则。

---

### M-4 `HARAIStatus` 匿名可访问，暴露 AI 服务指纹

**文件**: `web/handlers/har.py:611-621`  
**修复状态**: ⚠️ 未修（受 handler 限制）

**问题**: `/har/ai_status` 无需认证即可访问，返回 `{"enabled": true, "model": "gpt-4o-mini"}`。模型��可泄露底层服务商（OpenAI/DeepSeek/Moonshot 等），攻击者可据此定向构造 prompt injection 攻击或估算 token 成本。  
**建议**: 加 `@tornado.web.authenticated`，或不返回 `model` 字段（仅返回 `enabled`）。

---

### M-5 `sanitize_storage_state` 中 cookie `domain` 字段未做非字符串类型防御（已修复）

**文件**: `services/playwright/security.py:domain_matches`  
**修复状态**: ✅ 已修

**问题**: 原始 `domain_matches` 仅用 `if not cookie_domain` 检查空值，未处理 `None`（JSON null）、纯 `...` 型字符串、整数等非字符串类型。恶意构造的 `storage_state` JSON 可能利用类型强制绕过剔除逻辑。  
**修复**: 增加 `isinstance` 类型检查和纯 dots 字符串检测；并补充 11 个边界条件单元测试。

---

### M-6 `_aes_encrypt` / `_aes_decrypt` 支持 ECB 模式（开放接口）

**文件**: `libs/_utils/crypto.py:123-138`  
**修复状态**: ⚠️ 未修（接口变更）

**问题**: `switch_mode()` 支持 `ECB` 模式，且 `_aes_encrypt` 可被 Jinja2 模板表达式间接调用。ECB 模式不使用 IV，相同明文始终产生相同密文，不满足现代加密语义安全性要求。  
**说明**: 主密码存��路径���`db/user.py::encrypt/decrypt`）强制使用 CBC+随机 IV，不受影响。ECB 暴露面限于用户在 HAR 模板中显式调用 `_aes_encrypt(data, key, 'ECB')`。  
**建议**: 在 `switch_mode()` 中为 ECB 添加文档注释，说明不应用于敏感数据；或在 `_aes_encrypt` 文档中明确弃用 ECB。

---

## 低优 / 信息（🟢）

### L-1 `traceback_print` 生产环境若开启，栈帧可能含密钥

**文件**: `config.py:222-224`

默认在非 debug 模式下为 `False`，可接��。若运维人员手动设置 `TRACEBACK_PRINT=True` 在生产环境，异常日志可能含 `config.cookie_secret` 字节值（十六进制形式出现��内存表示中）。这属于运���配置风险，不是代码缺陷。

---

### L-2 FastAPI `auth.py` 的 `decode_signed_value` 注释与实现有轻微偏差

**文件**: `web/fastapi/auth.py:177`（`claude/fastapi-foundation` 分支）

```python
rest = raw[2:]  # strip b"2"
```

注释说"strip b\"2\""，实际剥离了 `b"2|"` 两字节，行为是正确的，但注释误导性。

---

### L-3 登录失败仅 `evil(+5)`，无账户锁定机制

**文件**: `web/handlers/login.py:81`, `web/handlers/base.py:107-115`

evil 机制基于 IP+userid 的 1 小时滑动窗口。Redis 不可用时 `is_evil()` 返回 `False`��fail-open）。在 Redis 不可用的环境下���力破解无法被阻断。这是已知的架构选择，文档中应注明 Redis 对安全的重要性。

---

### L-4 `safe_eval` 沙箱完整性

**文件**: `libs/safe_eval.py`

使用字节码��名单 + `_SAFE_OPCODES` 过滤，不依赖 Jinja2 `SandboxedEnvironment`（后者用于 `fetcher.py` 的模板渲染，与 safe_eval 是两套独���机制）。  
`safe_eval` 中 `timeout` 通过 `signal.SIGALRM`（非 Windows）或 `TerminableThread`（Windows）实现，对 CPU 炸弹有一定防护。  
字节码白名单方案已知有 Python 版本间差异风险（新 opcode 未在白名单中时会抛 ValueError），但这属于可接受的防御性失败模式（fail-close）。无发现主动逃逸风险。

---

### L-5 PBKDF2 iterations 默认 600000，符合现代标准

**文件**: `config.py:59`

OWASP 2023 推荐 PBKDF2-SHA256 使用 600,000 次，当前默认值与此一致。  
注意：PBKDF2 相比 Argon2id 内存硬度不足，但属于升级工程，非紧急。

---

### L-6 HAR 内容送 LLM 时 cookie ��已脱敏

**文件**: `libs/ai_client.py:_slim_entry:176`

`_slim_entry` 仅��集 `cookieNames`（cookie 名列表），不含 cookie 值，符合最小化原则。  
`Authorization` 和 `X-CSRF-Token` header 值会被送入 LLM（因为在 `keep_headers` 白名单中，且未截断 key，仅截断超长 value）。如果 HAR 中含有效 Bearer token，它会被送给第三方 LLM。这是功能必要信息，但应在用户界面给出明确提示。

---

## 已知 limit（���计选择，不视为 bug）

| 项目 | 说明 |
|------|------|
| ��全局 XSRF 保护 | Tornado 的 `xsrf_cookies` 未启用；FastAPI 版同样�� CSRF 中间件。这是贯穿全项目的一致���择，修改需要全局架构变更。 |
| `ALLOW_HOSTS` 默认空 | Playwright sidecar 的 URL 白名单依赖部署者配置，已有 WARNING 日志。 |
| fail-open Redis | Redis 不可用时 evil 限制失效，目的是保证可用性。 |
| HTTP 模式无 cookie secure | `cookie_secure_mode=False` 为默认值，HTTP 部署场景的向后兼容选择。 |
| ECB 模式可选 | 仅限用户显式调用的模板辅助函数，不用于���部密码存储。 |

---

## Phase 1 落地���汇总

| 编号 | 文件 | 变���摘要 |
|------|------|---------|
| P1-1 | `libs/ai_client.py` | 4xx 错误及 structure error 不再 echo 原始响应体给调用方；原始������降级为 debug 日志 |
| P1-2 | `run.py` | `_check_default_secrets` 新增 SMTP 无认证警告（MAIL_SMTP 已设但 MAIL_PASSWORD 为空时告警）|
| P1-3 | `services/playwright/security.py` | `domain_matches` 增加 non-str、None���纯 dots 的防御；补充注释 |
| P1-4 | `tests/test_security_fixes.py` | 新增 28 个回归测试（domain_matches、sanitize_storage_state、_check_default_secrets、AIClient 错误脱敏）|

---

## 未做的（记��原因）

| 项目 | 原因 |
|------|------|
| handler 认证修复（HARAIStatus、HARAutoCaptureStatus）| 受限于"不动 handler 文件"约束 |
| 全局 CSRF middleware | 架构变更，影响所有 POST 接口 |
| 改为 Argon2id 密码 hash | 破坏现有密码哈希向后兼容性 |
| `safe_eval` 沙箱重写 | 业务��达式语义依赖现有行为 |
| 默认 `cookie_secure_mode=True` | 破坏 HTTP-only 部署 |
| ECB 模式从接��中移除 | 接口变更，可能影响现有模板 |

---

## 建议下一步���按优先级）

1. **【P0 - 本周】** 在 `HARAIStatus` 和 `HARAutoCaptureStatus` 加 `@tornado.web.authenticated`，防��匿名信息泄露。（1 行改动，风险��低）
2. **【P1 - 本月】** 生产部署��档中将 `ALLOW_HOSTS` 标为强制要求，并考虑在 `validate_url` 中添加 RFC 1918 地址段拒绝规则（可选 opt-out）。
3. **【P1 - 本月】** 在 UI 层（HAR 编辑器）对"发送 HAR 给 AI 分析"功能加隐��提示，说明 Authorization header 中的 token 会被送往第三方 LLM。
4. **【P2 - 季度】** 引入 CSRF 双提交 cookie 或 SameSite=Strict 策略（结合 FastAPI 移植一起实施，减少破坏面）。
5. **【P2 - 季度】** 为 Redis 不可用场景增加��地内存 fallback 限速（如 `cachetools.TTLCache`），消除 fail-open 风险。
6. **【P3 - 年度】** 将密码 hash 迁移至 Argon2id（需要登录时透明升级哈希）。

---

## FastAPI `auth.py` 安全评估结论

`web/fastapi/auth.py`（`claude/fastapi-foundation` 分支）精确复现了 Tornado v2 secure cookie 格式，使用 `hmac.compare_digest` 防时序攻击，设置了 `httponly=True`、`samesite="lax"`，并尊重 `config.cookie_secure_mode`。

**未引入新风险**的方面：
- HMAC-SHA256 签名算法、字段解析逻辑、时间戳验证与 Tornado 完全对齐
- `decode_signed_value` 对格式���误���一返回 `None`，不暴露内部解析异常
- `get_current_user` 的 umsgpack 解码错误仅��录 debug 日志

**现有��知 limit 延续**：
- FastAPI 版同样没有 CSRF 保护（与 Tornado 版一致，不�� regression）
- `max_age_days` 读取 `config.cookie_days`（默认 5 天）���与 Tornado 版保持一致

**建议（非紧急）**：  
`web/fastapi/base.py::evil_counter` 中 Redis 不可用时 fail-open（与 Tornado 版一致），如果 FastAPI 版未来独立部署，建议明确文档化这一行为。
