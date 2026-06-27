# LinkAI（link-ai.tech）每日自动签到

> 用 QD 给 [LinkAI](https://link-ai.tech)（极简未来 一站式 AI 智能体平台）做每天自动签到（领积分）。本文提供一份开箱即用的 QD 模板，填账号密码、建一个每日定时任务即可。

## 一、原理

LinkAI 控制台是登录后的应用，签到接口需要带 `Authorization: Bearer <token>`。token 通过账号密码登录获取。所以签到是**两步**：

1. **POST** `https://link-ai.tech/api/login`，body `username=...&password=...` → 从返回里提取 `token`。
2. **GET** `https://link-ai.tech/api/chat/web/app/user/sign/in`，带 `Authorization: Bearer <token>` → 完成签到。

模板把这两步和 token 的提取/传递都写好了。**每次任务都重新登录拿新 token，所以不用担心 token 过期**——你只要填 `username` / `password`。

## 二、导入签到模板

模板文件：[`templates/linkai-signin.json`](https://github.com/s-silt/qd/blob/master/templates/linkai-signin.json)。

1. 把模板文件内容保存成 `linkai-signin.json`（内容见仓库该文件，或下方代码块）。
2. 登录 QD → `我的模板` 右侧 `+` → 选择该文件 → `上传`。
3. 进入编辑器后，右侧变量面板会出现 `username` 和 `password` 两个变量（`token` 是运行时自动提取的，不用填）。

<details>
<summary>展开查看模板 JSON</summary>

```json
[
  {
    "comment": "第1步：用账号密码登录 LinkAI，提取 token。需要变量 username / password。",
    "request": {
      "method": "POST",
      "url": "https://link-ai.tech/api/login",
      "headers": [
        {"name": "Content-Type", "value": "application/x-www-form-urlencoded;charset=UTF-8"},
        {"name": "Accept", "value": "application/json, text/plain, */*"},
        {"name": "Origin", "value": "https://link-ai.tech"},
        {"name": "Referer", "value": "https://link-ai.tech/home"},
        {"name": "User-Agent", "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.9 Safari/537.36"}
      ],
      "cookies": [],
      "data": "username={{username|urlencode}}&password={{password|urlencode}}",
      "mimeType": "application/x-www-form-urlencoded;charset=UTF-8"
    },
    "rule": {
      "success_asserts": [
        {"re": "\"token\"\\s*:\\s*\"[^\"]+\"", "from": "content"}
      ],
      "failed_asserts": [
        {"re": "用户名或密码|密码错误|账号或密码|用户不存在|账号不存在|登录失败|账户被锁定|验证码|too many|操作频繁", "from": "content"}
      ],
      "extract_variables": [
        {"name": "token", "re": "\"token\"\\s*:\\s*\"([^\"]+)\"", "from": "content"}
      ]
    }
  },
  {
    "comment": "第2步：带 token 调用签到接口领积分。",
    "request": {
      "method": "GET",
      "url": "https://link-ai.tech/api/chat/web/app/user/sign/in",
      "headers": [
        {"name": "Accept", "value": "application/json, text/plain, */*"},
        {"name": "Authorization", "value": "Bearer {{token}}"},
        {"name": "Origin", "value": "https://link-ai.tech"},
        {"name": "Referer", "value": "https://link-ai.tech/home"},
        {"name": "User-Agent", "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.9 Safari/537.36"}
      ],
      "cookies": []
    },
    "rule": {
      "success_asserts": [
        {"re": "签到成功|已签到|已经签到|今日已|重复签到|\"code\"\\s*:\\s*200|\"success\"\\s*:\\s*true", "from": "content"}
      ],
      "failed_asserts": [
        {"re": "未登录|登录已失效|登录失效|请重新登录|令牌|token 失效|未授权|无权限|unauthorized|<!DOCTYPE html", "from": "content"}
      ],
      "extract_variables": [
        {"name": "__log__", "re": "\"message\"\\s*:\\s*\"([^\"]*)\"", "from": "content"}
      ]
    }
  }
]
```

</details>

## 三、填写账号密码并测试

1. 在编辑器右侧变量面板，填入 LinkAI 的 `username`（登录用的手机号/邮箱）和 `password`。
2. 点编辑器底部的整体 `测试`（两步都会跑）。
3. 看结果：
   - 第1步拿到 `token`、第2步返回 `签到成功，获得 X 积分` → 成功 ✅
   - 返回 `今日已签到` / `您今天已经签到过了` → **也算成功**（模板已把已签到判为成功）。
   - 第1步提示 `用户名或密码错误` → 账号密码不对。
   - 第2步提示 `登录已失效` / `未授权` → 一般是 token 没取到，检查第1步登录是否成功。
4. 测试通过后点 `保存`，站点名建议填 `LinkAI`。

> 🔒 此模板会把 LinkAI 密码作为任务变量保存在 QD 里（QD 对变量按账号加密存储）。请确保你的 QD 实例只有自己能访问；不要把填好密码的模板/任务导出分享。

## 四、创建每日定时任务

1. `我的任务` 右侧 `+` → 选择 LinkAI 模板。
2. 填入 `username` / `password`。
3. Crontab 执行时间，例如 `0 9 * * *`（每天上午 9 点）。建议开启随机延迟。
4. 点 `测试` 确认能跑通 → `保存`。

之后 QD 每天自动登录并签到，结果在任务日志里查看（日志是 LinkAI 返回的签到消息，如「签到成功，获得 X 积分」）。

## 五、消息推送通知

在 QD 的 `用户` → 推送设置里配置推送方式（Server酱 / Bark / Telegram / 邮件 等），即可每天收到签到结果。详见 [推送工具](../toolbox/pusher.md)。

## 六、常见问题

**Q: 登录提示需要验证码 / 用了第三方（微信/Google）登录？**
本模板走的是账号密码登录接口。如果你的账号只能用扫码/第三方登录、或登录触发了验证码，账号密码方式可能不可用。可改为「token 直填」方案：删掉第1步，直接把浏览器里抓到的 `Bearer` token 作为变量填到第2步——但要注意 token 会过期，需定期更新。

**Q: token 提取失败 / 第2步总是未授权？**
说明第1步登录返回的结构变了或登录没成功。在测试面板看第1步响应里 `token` 字段的实际位置，必要时调整第1步的 `token` 提取正则。

**Q: 签到接口路径变了？**
若 LinkAI 改了接口，把模板里的 `/api/login` 或 `/api/chat/web/app/user/sign/in` 按新抓包结果改掉即可；也可以用「AI 智能识别签到」重新抓包生成。

**Q: 日志显示「今日已签到」算失败吗？**
不算，这是正常情况（今天已经签过了），模板已判为成功。

## 七、相关链接

- 模板文件：[`templates/linkai-signin.json`](https://github.com/s-silt/qd/blob/master/templates/linkai-signin.json)
- [NodeSeek 每日签到](./nodeseek-checkin.md) · [恩山论坛每日签到](./right-enshan-checkin.md)
- [如何使用 QD](./how-to-use.md) · [AI 自动生成签到模板](./ai-sign-template.md) · [URL 自动抓包](./auto-capture.md)
- [常见问题](./faq.md)
