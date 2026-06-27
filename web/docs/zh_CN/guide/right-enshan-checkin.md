# 恩山无线论坛（right.com.cn）每日自动签到

> 用 QD 给 [恩山无线论坛](https://www.right.com.cn/forum/) 做每天自动签到（领恩山币/积分）。本文提供一份开箱即用的 QD 模板，配置好 Cookie、建一个每日定时任务即可。

## 一、原理

恩山是 **Discuz! 论坛**，签到用的是 `erling_qd` 签到插件，且必须带一个**每次会话都不同的 `formhash`（CSRF 令牌）**。所以签到是**两步**：

1. **GET** 签到页 `https://www.right.com.cn/forum/erling_qd-sign_in.html` → 从 HTML 里提取 `formhash`。
2. **POST** `https://www.right.com.cn/forum/plugin.php?id=erling_qd:action&action=sign`，body 带上 `formhash=<上一步的值>`。

本模板已经把这两步和 `formhash` 的自动提取/传递都写好了，你只需要填 Cookie。

> ⚠️ **恩山站点有 WAF（加速乐）防护**，请务必阅读 [第六节](#六waf加速乐说明重要)，否则 GET 签到页就可能被拦、拿不到 formhash。

---

## 二、获取恩山 Cookie

模板需要一个变量 `cookie`，即你登录恩山后浏览器里的完整 Cookie。**关键：先在浏览器里正常浏览一下恩山论坛**（让 WAF 放行、把加速乐的 clearance Cookie 种下来），再导出 Cookie。

1. 用浏览器登录 <https://www.right.com.cn/forum/>，随便点开几个板块/帖子（确保不是停在验证页）。
2. 按 `F12` → **Network / 网络** 面板 → `F5` 刷新。
3. 点列表里任意一条发往 `right.com.cn` 的请求 → 右侧 **Request Headers / 请求标头** → 找到 `Cookie:` 这一行。
4. 把冒号后面**一整串**复制下来（要包含登录态和 WAF 的 `__jsl_clearance_s`、`__jsluid_h` 等）。

> 🔑 恩山的 Cookie 必须**完整**：既要有 Discuz 的登录 Cookie（`*_auth`、`*_saltkey` 等），也要有加速乐 WAF 的 clearance Cookie。只复制一半会导致拿不到 formhash 或被 WAF 拦。
> 💡 别用 `document.cookie`（登录态是 HttpOnly，读不到）；也别用 QD 自带的 `/get_cookies`（它开的是未登录的新浏览器）。
> 🔒 Cookie 等同登录态，不要外泄、不要提交到公开仓库。

---

## 三、导入签到模板

模板文件：[`templates/right-enshan-signin.json`](https://github.com/s-silt/qd/blob/master/templates/right-enshan-signin.json)。它是 QD 模板（JSON 数组格式），上传后会自动带上两步请求、断言与 `formhash` 提取规则。

1. 把模板文件内容保存成 `right-enshan-signin.json`（内容见仓库里的该文件，或下方代码块）。
2. 登录 QD → `我的模板` 右侧 `+` → 选择该文件 → `上传`。
3. 进入编辑器后，右侧变量面板会自动出现一个 `cookie` 变量（`formhash` 是运行时自动提取的，不用填）。

<details>
<summary>展开查看模板 JSON</summary>

```json
[
  {
    "comment": "第1步：打开恩山签到页(erling_qd 插件)，提取 formhash(CSRF)。",
    "request": {
      "method": "GET",
      "url": "https://www.right.com.cn/forum/erling_qd-sign_in.html",
      "headers": [
        {"name": "User-Agent", "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        {"name": "Accept", "value": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"},
        {"name": "Accept-Language", "value": "zh-CN,zh;q=0.9"},
        {"name": "Referer", "value": "https://www.right.com.cn/forum/forum.php"},
        {"name": "Cookie", "value": "{{cookie}}"}
      ],
      "cookies": []
    },
    "rule": {
      "success_asserts": [
        {"re": "name=\"formhash\"\\s+value=\"[0-9a-fA-F]+\"|action=logout(?:&amp;|&)formhash=", "from": "content"}
      ],
      "failed_asserts": [
        {"re": "您需要先登录|请先登录|未登录|action=login|加速乐|jsl_clearance|安全验证|请开启JavaScript", "from": "content"}
      ],
      "extract_variables": [
        {"name": "formhash", "re": "name=\"formhash\"\\s+value=\"([0-9a-fA-F]+)\"", "from": "content"}
      ]
    }
  },
  {
    "comment": "第2步：提交签到，data 里带上第1步抽取的 formhash。",
    "request": {
      "method": "POST",
      "url": "https://www.right.com.cn/forum/plugin.php?id=erling_qd:action&action=sign",
      "headers": [
        {"name": "User-Agent", "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        {"name": "Accept", "value": "application/json, text/javascript, */*; q=0.01"},
        {"name": "Accept-Language", "value": "zh-CN,zh;q=0.9"},
        {"name": "Content-Type", "value": "application/x-www-form-urlencoded; charset=UTF-8"},
        {"name": "X-Requested-With", "value": "XMLHttpRequest"},
        {"name": "Origin", "value": "https://www.right.com.cn"},
        {"name": "Referer", "value": "https://www.right.com.cn/forum/erling_qd-sign_in.html"},
        {"name": "Cookie", "value": "{{cookie}}"}
      ],
      "cookies": [],
      "data": "formhash={{formhash}}",
      "mimeType": "application/x-www-form-urlencoded; charset=UTF-8"
    },
    "rule": {
      "success_asserts": [
        {"re": "\"success\"\\s*:\\s*true|\"status\"\\s*:\\s*1|签到成功|已签到|已经签到|签到过|今天已|今日已", "from": "content"}
      ],
      "failed_asserts": [
        {"re": "未登录|请先登录|来路不正确|令牌错误|表单令牌|非法请求|操作频繁|系统繁忙|加速乐|jsl_clearance|安全验证|<!DOCTYPE html|<html", "from": "content"}
      ],
      "extract_variables": [
        {"name": "__log__", "re": "\"message\"\\s*:\\s*\"(.*?)\"", "from": "content"}
      ]
    }
  }
]
```

</details>

---

## 四、填写 Cookie 并测试

1. 在编辑器右侧变量面板，把第二节拿到的完整 Cookie 粘贴到 `cookie` 变量里。
2. 点编辑器底部的整体 `测试`（两步都会跑）。
3. 看结果：
   - 第1步能提取到 `formhash`、第2步返回 `"success":true` 或 `签到成功` → 成功 ✅
   - 返回 `您今天已经签到过了` / `今天已签到` → **也算成功**（模板已把已签到判为成功）。
   - 第1步就失败、提示 `加速乐` / `安全验证` / 一堆 HTML → 被 WAF 拦了，见第六节。
   - 提示 `未登录` / `来路不正确` → Cookie 不完整或已失效，重新获取完整 Cookie。
4. 测试通过后点 `保存`，模板信息里站点名建议填 `恩山论坛`。

---

## 五、创建每日定时任务

1. `我的任务` 右侧 `+` → 选择恩山模板。
2. 填入 `cookie` 变量。
3. Crontab 执行时间，例如 `0 9 * * *`（每天上午 9 点）。建议**开启随机延迟**、错开整点。
4. 点 `测试` 确认能跑通 → `保存`。

之后 QD 会每天自动签到，结果在任务日志里查看（日志是恩山返回的签到消息）。

---

## 六、WAF（加速乐）说明（重要）

恩山站点有加速乐 WAF（被拦时常见状态码 `521`，返回一段需要执行 JavaScript 的 HTML 验证页）。QD 是纯 HTTP 客户端、不执行 JS，所以应对方式是：

1. **Cookie 里带上加速乐 clearance（最关键）**
   先在浏览器里正常访问恩山、通过验证后，再导出**完整 Cookie**（含 `__jsl_clearance_s`、`__jsluid_h`）。只要这些 clearance Cookie 没过期，QD 的请求就能被 WAF 放行。
2. **使用 JA3 / curl-impersonate 镜像**
   本仓库的 [`Dockerfile.ja3`](https://github.com/s-silt/qd/blob/master/Dockerfile.ja3) 会把 TLS/JA3 指纹伪装成真实 Chrome，能降低被 WAF 识别为脚本的概率（详见 [Docker 部署教程](./docker-deploy.md)）。
3. **加速乐 clearance 会过期**
   `__jsl_clearance_s` 有时效（可能几小时到几天）。如果某天开始持续签到失败、日志出现 `加速乐`/`安全验证`，说明 clearance 过期了，**重新到浏览器里走一遍、导出新的完整 Cookie** 更新到任务变量即可。建议把签到失败推送打开，第一时间能发现。
4. **必要时配置出口代理**
   服务器 IP 被风控时，可在任务变量里设置 `_proxy`（如 `http://user:pass@host:port`）。

---

## 七、消息推送通知

在 QD 的 `用户` → 推送设置里配置推送方式（Server酱 / Bark / Telegram / 邮件 / 自定义 等），即可每天收到签到结果。详见 [推送工具](../toolbox/pusher.md)。

强烈建议**打开失败推送**：恩山 Cookie / WAF clearance 会过期，失败时能及时收到提醒去更新 Cookie。

---

## 八、多账号

QD 一个任务对应一个账号。多个恩山账号就用**同一个模板**新建多个任务，各自填自己的完整 `cookie`，并设置不同的执行时间。

---

## 九、常见问题

**Q: 第1步提取不到 formhash？**
多半是没登录或 Cookie 不完整（被 WAF 拦在验证页，页面里根本没有签到表单）。重新在浏览器里访问恩山、通过验证、登录后，导出**完整 Cookie**（含加速乐 clearance）。

**Q: 一直 `521` / 返回一堆 HTML / 提示加速乐？**
WAF 拦截。按第六节：带全加速乐 clearance Cookie + 用 JA3 镜像；clearance 过期就重新导出 Cookie。

**Q: 提示 `来路不正确` / `令牌错误`？**
formhash 失效（通常是两步之间 Cookie 不一致或会话过期）。重新导出完整 Cookie 再试。

**Q: 日志显示 `您今天已经签到过了` 算失败吗？**
不算。模板已把已签到判为成功，这是正常情况。

**Q: 插件变了/签到地址变了怎么办？**
恩山若更换签到插件，把模板里两步的 URL（`erling_qd-sign_in.html` 与 `plugin.php?id=erling_qd:action&action=sign`）和 formhash 正则按新页面调整即可；也可以用「AI 智能识别签到」重新抓包生成。

---

## 十、相关链接

- 模板文件：[`templates/right-enshan-signin.json`](https://github.com/s-silt/qd/blob/master/templates/right-enshan-signin.json)
- [NodeSeek 每日签到](./nodeseek-checkin.md)（单步签到示例）
- [如何使用 QD](./how-to-use.md) · [Docker 部署教程](./docker-deploy.md)（含 JA3 镜像）
- [HAR 抓包教程](./har-capture.md) · [AI 自动生成签到模板](./ai-sign-template.md)
- [常见问题](./faq.md)
