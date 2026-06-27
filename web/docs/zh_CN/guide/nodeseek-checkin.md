# NodeSeek 每日自动签到

> 用 QD 给 [NodeSeek](https://www.nodeseek.com) 论坛做每天自动签到（领鸡腿）。本文提供一份开箱即用的 QD 模板，配置好 Cookie 后创建一个每日定时任务即可，无需自己抓包。

## 一、原理

NodeSeek 的签到就是一次带 Cookie 的 HTTP 请求：

```
POST https://www.nodeseek.com/api/attendance?random=true
```

- `random=true`：随机奖励，每天 1-10 个鸡腿（碰运气）。
- `random=false`：固定奖励，每天 5 个鸡腿（稳定）。

QD 会按你设定的定时表达式，每天自动发起这个请求，并把返回的签到结果记到任务日志里（还能推送到微信 / TG / 邮件等）。

> ⚠️ **NodeSeek 站点有 Cloudflare 防护**，请务必阅读 [第六节](#六cloudflare-说明重要)，否则可能签到失败。

---

## 二、获取 NodeSeek Cookie

模板需要一个变量 `cookie`，即你登录 NodeSeek 后浏览器里的完整 Cookie。任选一种方式：

1. **浏览器 DevTools（推荐）**
   1. 用浏览器登录 <https://www.nodeseek.com>。
   2. 按 `F12` 打开开发者工具 → `Network`（网络）面板，勾选 `Preserve log`。
   3. 刷新页面，点开任意一条发往 `nodeseek.com` 的请求，在 `Request Headers` 里找到 `Cookie:` 这一行，复制冒号后面的**整段内容**。
2. **QD 内置页面**：访问 QD 的 `/get_cookies/page`，输入 `https://www.nodeseek.com` 自动提取（需要部署的镜像带 Playwright）。
3. **浏览器扩展**：安装 [QD get-cookies 扩展](https://github.com/qd-today/get-cookies)，在 NodeSeek 页面一键复制。

Cookie 形如（一长串，至少包含 `session=...`）：

```
session=MTxxxxxxxx...; ...; cf_clearance=yyyyyyyy...
```

> 🔑 Cookie 里最关键的是 `session`。如果还带了 `cf_clearance`，请连同它一起复制（用于通过 Cloudflare，见第六节）。
> 🔒 Cookie 等同于你的登录态，**不要分享给任何人、不要提交到公开仓库**。

---

## 三、导入签到模板

模板文件：[`templates/nodeseek-signin.json`](https://github.com/s-silt/qd/blob/master/templates/nodeseek-signin.json)。它是一个标准的 QD 模板（JSON 数组格式），上传后会自动带上断言与日志规则，无需手工设置。

### 方式 A：上传模板文件

1. 把下面的内容保存成一个文件，例如 `nodeseek-signin.json`：

   ```json
   [
     {
       "comment": "NodeSeek 每日签到。?random=true 随机鸡腿(1-10个), 改成 false 则固定 5 个。需要变量 cookie(浏览器登录后的完整 Cookie)。",
       "request": {
         "method": "POST",
         "url": "https://www.nodeseek.com/api/attendance?random=true",
         "headers": [
           {"name": "User-Agent", "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"},
           {"name": "Accept", "value": "*/*"},
           {"name": "Accept-Language", "value": "zh-CN,zh;q=0.9,en;q=0.8"},
           {"name": "Origin", "value": "https://www.nodeseek.com"},
           {"name": "Referer", "value": "https://www.nodeseek.com/board"},
           {"name": "Cookie", "value": "{{cookie}}"}
         ],
         "cookies": []
       },
       "rule": {
         "success_asserts": [
           {"re": "\"success\"\\s*:\\s*true|已完成签到|重复操作", "from": "content"}
         ],
         "failed_asserts": [
           {"re": "USER NOT FOUND|未登录|请先登录|无法验证|登录已过期|Just a moment|Attention Required|cf-browser-verification|<!DOCTYPE html", "from": "content"}
         ],
         "extract_variables": [
           {"name": "ns_gain", "re": "\"gain\"\\s*:\\s*(\\d+)", "from": "content"},
           {"name": "ns_current", "re": "\"current\"\\s*:\\s*(\\d+)", "from": "content"},
           {"name": "__log__", "re": "\"message\"\\s*:\\s*\"(.*?)\"", "from": "content"}
         ]
       }
     }
   ]
   ```

2. 登录 QD → 点击 `我的模板` 右侧的 `+` 按钮 → 选择刚保存的 `nodeseek-signin.json` → 点 `上传`。
3. 进入 HAR 编辑器后，右侧变量面板会自动出现一个 `cookie` 变量。

### 方式 B：手动新建

如果不想上传文件，也可以新建一个空模板，在编辑器里手动添加上面这一条请求和规则。AI 镜像用户还可以直接点「AI 智能识别签到」自动生成。

---

## 四、填写 Cookie 并测试

1. 在 HAR 编辑器右侧变量面板，把第二节拿到的 Cookie 粘贴到 `cookie` 变量里。
2. 点击请求卡片上的 `测试`（或编辑器底部的整体 `测试`）。
3. 看响应内容（NodeSeek 返回的是 JSON）：
   - 出现 `"success":true`（并带 `"gain"`/`"current"` 字段）→ 签到成功 ✅，鸡腿数量看 `gain`（本次获得）和 `current`（当前总数）。
   - 出现 `今天已完成签到，请勿重复操作` → 今天已签过，**也算成功**（模板已把它判为成功，不会误报失败）。
   - 出现 `USER NOT FOUND`（HTTP 404）或 `未登录` / `登录已过期` → Cookie 不对或已失效，重新获取。
   - 出现一堆 HTML / `Just a moment` → 被 Cloudflare 拦截，见第六节。
4. 测试通过后点 `保存`，并在模板信息里把站点名填成 `NodeSeek`（方便识别）。

---

## 五、创建每日定时任务

1. 点击 `我的任务` 右侧的 `+` → 选择刚保存的 NodeSeek 模板。
2. 填入 `cookie` 变量值。
3. 设置执行时间，用 Crontab 表达式，例如：
   - `0 8 * * *` —— 每天早上 8:00 签到。
   - `30 0 * * *` —— 每天凌晨 0:30 签到（刚过零点，抢首签）。
4. 建议**开启随机延迟**：在时间设置里选支持随机延迟的模式，把延迟区间设成例如 `0 ~ 1800` 秒，避免所有人卡同一秒请求（更像真人，也更稳）。
5. 点 `测试` 确认任务能跑通 → `保存`。

完成后，QD 会每天自动签到，结果可在 `我的任务` → 对应任务的日志里查看（日志内容就是 NodeSeek 返回的签到消息）。

> 💡 任务失败时，QD 会按退避策略在当天多次重试（间隔逐渐拉长，约 10 分钟 → 数小时），所以同一天可能看到多条失败日志，这是重试不是重复签到。NodeSeek 的登录态通常能维持数周，失效后需要重新获取 Cookie。

---

## 六、Cloudflare 说明（重要）

NodeSeek 在 Cloudflare 后面。普通 HTTP 客户端（包括默认的 QD 镜像）发出的 TLS 指纹会被识别成「非浏览器」，可能被 Cloudflare 拦截，表现为返回一个 `Just a moment...` 的 HTML 页面而不是 JSON。应对方法（按推荐度排序）：

1. **使用 JA3 / curl-impersonate 镜像（强烈推荐）**
   本仓库提供 [`Dockerfile.ja3`](https://github.com/s-silt/qd/blob/master/Dockerfile.ja3)，它内置 `curl-impersonate`，会把 TLS/JA3 指纹伪装成真实 Chrome，绝大多数情况下能直接通过 Cloudflare。部署时改用该镜像即可（详见 [Docker 部署教程](./docker-deploy.md)）。
2. **带上 `cf_clearance` Cookie**
   如果仍被拦，登录后从浏览器里把 `cf_clearance` 一并复制进 `cookie` 变量。注意：
   - `cf_clearance` 与浏览器的 **User-Agent 绑定**。如果你提供了 `cf_clearance`，请把模板里 `User-Agent` 这个 header 改成**你自己浏览器的 UA**（在 DevTools 同一条请求的 Request Headers 里能看到），两者必须一致，否则无效。
   - `cf_clearance` 还与**签发它的 IP 绑定**。在你家浏览器里拿到的 clearance，到了服务器（不同出口 IP）通常不被认可——所以更推荐用 JA3 镜像；若配了 `_proxy`，clearance 也得是从该代理出口 IP 取得的。
   - `cf_clearance` 有有效期（通常几十分钟到数小时不等），过期后该手段会失效，但只要 JA3 镜像够用，一般不需要它。
3. **配置出口代理**
   若服务器 IP 被 Cloudflare 风控，可在任务变量里设置 `_proxy`（如 `http://user:pass@host:port`）走一个干净的住宅/机房代理。

---

## 七、随机签到 vs 固定签到

模板默认 `?random=true`（随机 1-10 个鸡腿）。想要稳定的 5 个，把请求 URL 改成：

```
https://www.nodeseek.com/api/attendance?random=false
```

二选一，每天只能签一次。

---

## 八、消息推送通知

想每天收到签到结果通知（成功 / 失败 / 鸡腿数）？

1. 在 QD 的 `用户` → 推送设置里配置好推送方式（支持 Server酱 / Bark / Telegram / 邮件 / 自定义 等）。
2. 失败推送默认开启：把「错误容忍次数」保持为 `0`，任务第一次失败就会推送（Cookie 失效时能立刻收到）。成功推送是单独的开关，想每天收到「已签到 + 鸡腿数」就把它也打开。
3. 推送内容会带上任务日志（即 NodeSeek 的签到消息）。

详见 [推送工具](../toolbox/pusher.md)。

---

## 九、多账号

QD 一个任务对应一个账号。多个 NodeSeek 账号时，用**同一个模板**新建多个任务，每个任务填各自的 `cookie` 即可。建议给每个任务设置不同的执行时间 / 随机延迟。

---

## 十、常见问题

**Q: 一直返回 `Just a moment...` / 一堆 HTML？**
被 Cloudflare 拦了。改用 JA3 镜像（第六节方法 1），必要时叠加 `cf_clearance` + 匹配的 UA。

**Q: 提示 `USER NOT FOUND` / `未登录` / `登录已过期`（HTTP 404）？**
Cookie 失效了。NodeSeek 的 `session` 会过期（通常能维持数周），重新登录获取一遍 Cookie 更新到任务变量里。建议把失败推送打开（错误容忍次数设 0），失效时第一时间能收到通知。

**Q: 日志显示「今天已完成签到，请勿重复操作」算失败吗？**
不算。模板已把「已签到」判定为成功，这是正常情况（说明今天已经签过了）。

**Q: 想看签到拿了多少鸡腿？**
任务日志里就是 NodeSeek 的原始消息。模板还额外抽取了 `ns_gain`（本次获得）和 `ns_current`（当前总数）两个变量备用。注意：这两个字段只在「当天首次签到成功」的响应里有；若当天已签过（返回「已完成签到」），它们会为空，属正常现象。

**Q: 会被封号吗？**
自动签到属于薅论坛福利，风险自担。建议：① 用随机延迟、别卡整点；② 不要一个 IP 挂几十个号；③ 遵守 NodeSeek 站点规则。

---

## 十一、相关链接

- 模板文件：[`templates/nodeseek-signin.json`](https://github.com/s-silt/qd/blob/master/templates/nodeseek-signin.json)
- [如何使用 QD](./how-to-use.md)
- [Docker 部署教程](./docker-deploy.md)（含 JA3 镜像说明）
- [HAR 抓包教程](./har-capture.md) · [AI 自动生成签到模板](./ai-sign-template.md) · [URL 自动抓包](./auto-capture.md)
- [常见问题](./faq.md)
