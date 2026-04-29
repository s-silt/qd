# URL 自动抓包（Playwright）

> 在 [HAR 抓包教程](./har-capture.md) 与 [AI 自动生成签到模板](./ai-sign-template.md) 的基础上，本功能进一步把「打开浏览器、点击签到按钮、导出 HAR」这一步自动化：用户只要给 URL + 登录态（Cookie 或 storage_state），QD 就用 Playwright 在容器里跑完整流程并自动喂给 AI 分析，直接产出可保存的模板。

## 一、架构与权衡

```
┌──────────────┐    POST URL+Cookie    ┌──────────────────────┐
│ QD HAR 编辑器 │  ───────────────────► │ Playwright sidecar   │
│ "URL 自动抓包" │                       │ (独立容器, ~1.5GB)    │
└──────┬───────┘                       │  1. 注入 cookie       │
       │                               │  2. 加载页面          │
       │                               │  3. 启发式找签到按钮  │
       │  HAR + 候选按钮                │  4. 点击 → 录制 HAR   │
       │ ◄─────────────────────────────│                       │
       │                               └──────────────────────┘
       ▼ (auto_analyze=true 时)
  /har/ai_analyze (复用 AI 识别) → 最小化 HAR → 模板
```

- **登录步骤交还给用户**：你在自己的浏览器登录后，把 Cookie 贴给 QD（一次性动作）。这是稳定性最高的方案，规避验证码 / 风控 / 2FA。
- **找签到按钮的策略**（按顺序）：
  1. 用户手填的 CSS selector
  2. 文本启发式：DOM 内匹配 `签到 / 打卡 / 每日 / sign in / check-in / claim` 等关键字（同时降权 `登录 / 退出` 等噪声）
  3. 都失败：返回 top 10 候选按钮，用户挑一个填回 selector 重试
- **反检测最小集**：抹掉 `navigator.webdriver`、随机点击延迟、设置中文 locale + 中国时区
- **资源**：每次抓包约 5-30 秒、~250 MB RAM、并发由 `MAX_CONCURRENT` 限制

## 二、启用方法

### 2.1 docker-compose 部署（推荐）

[docker-compose.local.yml](../../../docker-compose.local.yml) 已自带 `playwright` 服务声明，启用步骤：

1. 取消 QD 服务下这两行注释：

   ```yaml
   - PLAYWRIGHT_SIDECAR_URL=http://playwright:8924
   - PLAYWRIGHT_CAPTURE_TIMEOUT=120
   ```

2. 重建并启动：

   ```bash
   docker compose -f docker-compose.local.yml up -d --build
   ```

   首次构建 sidecar 大约 3-8 分钟（要拉 ~1.5GB 镜像 + 装中文字体）。

3. 在 QD 主容器内验证：

   ```bash
   docker compose -f docker-compose.local.yml exec qd \
       wget -qO- http://playwright:8924/health
   # {"ok":true,"headless":true,"max_concurrent":2,"browser_ready":true}
   ```

### 2.2 不想要时如何关闭

- 完全不需要：把 `docker-compose.local.yml` 里整段 `playwright:` 服务注释掉，并保留 `PLAYWRIGHT_SIDECAR_URL` 注释。
- 临时停用：`docker compose stop playwright`。QD 里按钮会自动变灰。

### 2.3 调优环境变量

在 `playwright` 服务的 `environment:` 段添加：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HEADLESS` | `true` | debug 时设 `false`，但需要 X server |
| `MAX_CONCURRENT` | `2` | 并发抓包数；提高需要更多内存 |
| `DEFAULT_TIMEOUT_MS` | `60000` | 单次抓包总超时 ms |
| `ALLOW_HOSTS` | 空 | 域名白名单 `example.com,foo.com`，留空允许全部，**生产强烈建议设置** |

## 三、操作步骤

### 第 1 步：在浏览器登录目标网站

打开签到网站、登录到能看到签到按钮的状态。**不要登出**。

### 第 2 步：复制 Cookie

打开 DevTools（`F12`）→ **Application** 选项卡 → 左侧 **Storage → Cookies → 站点 URL**：

- **简单做法**：右键 → "Copy all as cURL"，从 cURL 命令里复制 `Cookie:` 头那一行
- **干净做法**：安装 [qd-today/get-cookies](https://github.com/qd-today/get-cookies) 浏览器扩展（仓库自行下载安装，不随 QD 一起分发），登录目标站点后在 QD 测试面板点"获取"按钮即可一键导出标准格式 cookie 字符串

### 第 3 步：使用 QD 自动抓包

1. QD 顶部 → **HAR 模板** → 新建 / 编辑
2. 右上角点 **「URL 自动抓包」**
3. 填写：
   - **URL**：签到页面的网址（不是首页！是看得到签到按钮的那个页面）
   - **Cookie**：刚才复制的 `k1=v1; k2=v2 ...`
   - **storage_state JSON**（可选）：如果你有完整 storage_state 文件就贴进来，会比 Cookie 更全（包括 localStorage）
   - **提示词**（推荐填）：`每日签到` / `积分领取` / `打卡` 等
   - **CSS Selector**（可选）：如果第一次跑后系统给出了候选清单，再回来填
   - **抓到 HAR 后自动调用 AI 分析**：默认勾选，需要先配置 `AI_API_KEY`
4. 点 **开始抓包**，等 5-30 秒
5. 看到结果：
   - ✅ 找到签到按钮 → 直接点 **应用为当前模板**
   - ❌ 没找到 → 看下方候选按钮列表，点击文字应用为 selector，再 **开始抓包** 一次

### 第 4 步：补充变量并测试

跟手动 HAR 流程一样：右侧面板填 cookie 等变量 → **测试** → **保存** → 创建定时任务。

## 四、与已有功能的关系

| 功能 | 何时使用 |
| --- | --- |
| **传统 HAR 上传** | 已熟悉抓包，想精确控制每一条请求 |
| **AI 智能识别签到** (`/har/ai_analyze`) | 已有 HAR 文件，想自动剔除噪声 |
| **URL 自动抓包**（本功能） | 不想自己开浏览器抓包，希望全程自动化 |

三者可以叠加：自动抓包 → 自动喂给 AI → 应用为模板 = 一气呵成。

## 五、Cookie 失效与维护

Cookie 不是永久的（短则一天、长则数月）。以下三种情况都会让自动抓包失败：

1. **Cookie 过期**：sidecar 检测到页面被重定向到 `login` 类 URL，会返回错误「登录态可能已失效」
2. **网站升级反爬**：开始检测 `navigator.webdriver` 之外的指纹（Canvas、字体、WebGL）—— 极少站点会这样，可以在 sidecar 里加 `playwright-stealth`，但维护成本高
3. **风控弹窗**：有的站点偶尔会弹「请验证你不是机器人」滑块，这种我们不主动处理（机器无法稳定通过），需要你重新在浏览器里完成验证后再复制 Cookie

**建议**：把抓包当作「**生成模板的一次性动作**」，模板本身仍由原 QD 框架定时执行——一次抓包 = 多次签到，Cookie 失效就重抓一次。

## 六、安全注意

1. **Cookie 等同账号**：sidecar 容器和 QD 容器之间走内部网络（compose service name），**绝对不要把 sidecar 端口暴露到公网**。`docker-compose.local.yml` 里 `playwright` 服务用的是 `expose:` 而不是 `ports:`，已默认只对 compose 网络可见。
2. **建议设置 `ALLOW_HOSTS`**：限制 sidecar 只能访问指定 host，避免 SSRF（攻击者通过 `/har/auto_capture` 让 sidecar 访问内网服务）。
3. **传给 AI 的内容已脱敏**：HAR 喂给 AI 前会过滤——只保留必要 header，Cookie 只送名称，body 截断 500 字。详见 [AI 教程](./ai-sign-template.md#五ai-工作原理faq)。
4. **不要用别人的 Cookie**：抓包等同登录，对应的隐私和合规责任由 Cookie 持有人承担。

## 七、API 参考（开发者）

### 主端 `POST /har/auto_capture`

请求需要登录（XSRF token），Body：

```json
{
  "url": "https://example.com/sign",
  "cookies": "k1=v1; k2=v2",
  "storage_state": null,
  "hint": "每日签到",
  "selector": null,
  "auto_analyze": true
}
```

响应（成功）：

```json
{
  "ok": true,
  "har": { "log": { "entries": [...] } },
  "actions": [
    {"type": "navigate", "url": "..."},
    {"type": "click", "selector": "#daily-sign", "text": "立即签到"}
  ],
  "found_button": {"text": "立即签到", "selector": "#daily-sign"},
  "candidates": [...],
  "elapsed_ms": 8234,
  "ai": {
    "result": {"sitename": "...", "entries": [...]},
    "har": {"log": {"entries": [...]}}
  }
}
```

### Sidecar `POST /capture`

直接调用 sidecar（仅在 compose 网络内）：

```bash
curl -X POST http://playwright:8924/capture \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com","cookies":"k=v","hint":"签到"}'
```

完整 schema 见 [services/playwright/app.py](../../../services/playwright/app.py)。

## 八、相关文档

- [HAR 抓包教程](./har-capture.md)
- [AI 自动生成签到模板](./ai-sign-template.md)
- [Docker 部署教程](./docker-deploy.md)
