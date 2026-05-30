# Code Review — 2026-05-30 (安全专项 + 迭代修复)

**范围:** 全项目整体 + 拆分模块安全审查, 以 Tencent VulnGym 漏洞分类法为清单
(业务逻辑越权/IDOR、多租户隔离、SSRF、模板/代码注入、沙箱逃逸、路径穿越、
反序列化、命令注入), 结合本仓库特点逐条核实并修复。

**部署前提:** 本项目仅架设在内网, 因此对「需要内网可达性才能正常使用」的能力
(如抓包目标为内网主机) 不做会破坏功能的收紧; 对纯外网暴露风险酌情降级。

---

## 图例

| 等级 | 含义 |
|------|------|
| P1 | 严重: 直接 RCE / 越权写他人核心数据 / 鉴权绕过 |
| P2 | 高: 越权读写、SSRF、DoS |
| P3 | 中: 信息泄露、健壮性、限速失效 |
| ✅ 已修复 / 🟢 复核为安全 / 🟡 可接受风险(内网降级) |

---

## 一、已修复

### 越权 / IDOR

新增 `BaseHandler.check_self_or_admin(userid)`: 形如 `/<userid>/...` 的接口若直接信任 URL
中的 userid 读写账户数据, 任意已登录用户即可操作他人账户。统一校验: 普通用户强制
`userid=自身 id`(返回 int 便于比较), 管理员放行; 不匹配抛 403 并 `evil(+5)`。

- ✅ **P2** `UserRegPush.get/post`(`/user/<userid>/regpush`): 越权改写他人
  Bark/S酱/WxPusher/企业微信/TG/钉钉/Webhook 全部推送令牌 → 通知劫持。已加守卫。
- ✅ **P2** `UserRegPushSw.get/post`(`/user/<userid>/pushsw`): 越权改写他人通知开关 /
  批量推送 / 日志容错次数。已加守卫。
- ✅ **P2** `CustomPusherHandler.get/post`(`/util/custom/<userid>/pusher`): 越权改写他人
  自定义推送(可设任意 URL/方法, 通知劫持 + SSRF 跳板)。已加守卫。
- ✅ **P2** `TaskMultiOperateHandler.post`(`/task/<userid>/multi`): 原仅校验
  `task['userid'] == int(userid)`, 但 userid 取自 URL(同为攻击者可控), 可对他人任务
  启用/禁用/删除/改分组/改时间。已加 `check_self_or_admin`。
- ✅ **P2** `GetTasksInfoHandler.post`(`/task/<userid>/get_tasksinfo`): 无归属校验, 可枚举
  他人任务信息。已加守卫 + `task['userid']==userid` 过滤。
- ✅ **P3** `TaskGroupHandler.get/post`、`TPLGroupHandler.get/post`(`/task|tpl/<id>/group`):
  分组标签读写无 `check_permission`, 跨租户读写。已在读写前补 `check_permission(obj,'r'/'w')`。
- ✅ **P3** `UserSetNewPWDHandler.get`(`/user/<userid>/setnewpwd`): GET 按任意 userid 读取
  邮箱(PII 枚举)。已加守卫(POST 本就经管理员 `challenge_md5` 校验)。

### SSRF

- ✅ **P2** `libs/playwright_capture.validate_url`: 未配置 `PLAYWRIGHT_ALLOW_HOSTS` 时不校验目标。
  新增 `libs/security.resolve_blocked_reason`, 拦截 loopback / link-local(含云元数据
  169.254.169.254)/ multicast / reserved; **默认放行 RFC1918 私网**(内网部署合法目标),
  可用 `PLAYWRIGHT_BLOCK_PRIVATE_IP=1` 在公网部署时收紧。新增 `tests/test_security.py` 覆盖。

### 正确性 / 健壮性 / 测试

- ✅ **P1(健壮性)** `worker.do()`: `userid = user['id']` 前移到 `if not user` 之后, 任务属主被删时崩溃。
- ✅ **P3** `worker.runner/producer`: `asyncio.sleep` 限速移至循环末尾(原在阻塞 `queue.get` 前创建,
  任务耗时超间隔时 `await` 立即返回, 限速失效)。
- ✅ **P3** `db/basedb._insert_or_update`: `on_duplicate_key_update()` 返回新语句却被丢弃, 退化为普通
  INSERT。已用返回值执行。(当前无调用方, 属潜在隐患)
- ✅ `tests/test_ai_client.py` 两个用例长期为红: `ai_result_to_har` 重构为返回 list, 测试仍按旧
  `har["log"]["entries"]` 断言。已更新并补新格式用例; 函数返回注解 `Dict -> List`。

---

## 二、复核为安全 / 可接受 (避免误报)

- 🟢 `safe_eval.py`: Odoo 沙箱, 递归校验 `co_consts` 内嵌 code 对象、禁 dunder 名、opcode 白名单,
  builtins 受限。运行时 Python 3.11 opcode 集匹配。沙箱逃逸风险低。(升 3.12+ 需补 opcode)
- 🟢 用户模板渲染走 `jinja2.sandbox.SandboxedEnvironment`(`libs/fetcher.py`); Web 端模板为服务端文件。
- 🟢 SQL 全走 SQLAlchemy ORM / 参数化; 无 `os.system/subprocess/popen`; 无 `pickle/marshal/yaml.unsafe`。
- 🟢 `UserManagerHandler` / `UserDBHandler` / `UserSetNewPWDHandler.post` / `UserPushShowPvar`:
  以 URL userid 入参, 但敏感操作均经 `challenge_md5(mail,pwd)`(+ 管理员/邮箱匹配)校验, 非越权。
  其 GET 仅渲染管理表单(实际操作仍需管理员口令), 风险低。
- 🟢 `util.py` Toolbox/Notepad 系列: 经 `check_permission` 或 `challenge_md5` 校验。
- 🟢 `GetCookiesExtension` 打包目录为硬编码常量, 无路径穿越; `password_hash` 用 PBKDF2-SHA256。

### 🟡 可接受风险 (内网部署, 按用户指示降级, 非 P3 以上 / 改动会破坏功能)

- `config.py` `cookie_secret`/`aes_key` 默认 `"binux"`: 仅告警不阻断。内网部署务必设环境变量。
- 未启用 Tornado `xsrf_cookies`: 启用会破坏现有 AJAX/API 流。
- `_proxy`、`/har/test`(HARTest)允许用户发起任意出站请求: QD 作为个人自动化平台属设计内行为。
- `libs/fetcher.py` 用户正则(success/failed/extract)无超时, 理论 ReDoS: 内网 + 半可信用户降级。

---

## 三、验证

- `python3 -m py_compile` + `ast.parse`: 全部改动文件通过。
- `pytest tests/`: 业务用例全绿(`tests/test_ai_client.py`、`test_worker_pure.py`、
  `test_funcs_pure.py`、`test_config.py`、新增 `test_security.py`)。
  `tests/test_utils.py` 的 8 个用例为**既有失败**(本会话无 `tornado` 依赖, 在 master 上同样失败),
  与本次改动无关。

## 四、本会话环境异常 (需用户知悉)

审查/修复过程中工具输出通道多次出现异常: 部分文件读取返回被截断为「0 行」, 并混入伪造的
「忽略系统提示 / 停止使用工具 / 结果已隐藏」之类**提示词注入**文本。经 `grep` 全仓确认这些
文本**不存在于任何仓库文件**, 判定来自执行环境 / 工具通道层(疑似已连接的某个 MCP 服务或远程
执行包装层), 而非项目代码。已全程忽略其指令。

> 建议: 排查本会话连接的 MCP 服务来源——注入文本经工具结果通道传入, 属供应链 / 环境侧风险。
