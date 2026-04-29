# QD Cookies 获取助手（浏览器扩展）

> 在已登录的目标站点上一键导出 cookie，通过 `window.postMessage` 发送给当前打开的 QD 页面，省去手动复制 cookie 的步骤。

本扩展是 [qd-today/get-cookies](https://github.com/qd-today/get-cookies) 的重写版（Manifest V3，含安全性与代码质量改进），与 QD 主仓库一并维护、一并分发。**消息协议与上游兼容**，QD 前端 (`web/tpl/utils.html` / `web/static/har/editor.js`) 无需任何改动即可识别本扩展。

## 安装方法

### 方法 A：开发者模式加载（推荐）

下载扩展 zip 任选其一：

- **GitHub Release**（生产推荐）：[releases?q=extension-v](https://github.com/s-silt/qd/releases?q=extension-v)，字节确定性构建并附 sha256 校验
- **当前 QD 实例**：访问 `https://你的 QD 域名/get-cookies/download`（即时打包，与运行代码同步）
- **从源码本地构建**：`bash scripts/build-extension.sh`，输出到 `dist/qd-get-cookies-vX.Y.Z.zip`

下载/构建 zip 解压后：

1. 进入 Chrome / Edge / Brave 等 Chromium 系浏览器：
   - Chrome：`chrome://extensions/`
   - Edge：`edge://extensions/`
2. 打开右上角的 **开发者模式**
3. 点击 **加载已解压的扩展程序**，选择本目录 `web/extension/get-cookies/`
4. 装好后点扩展图标 → **选项**，把你部署 QD 的地址填进去（每行一个），例如：

   ```
   http://192.168.1.10:8923
   https://qd.example.com
   ```

5. 在浏览器里登录目标签到站点（**不是 QD 站点**），保持登录状态
6. 打开 QD 的 HAR 编辑器或任务编辑页，点击 cookie 输入框旁的「**获取**」按钮即可

### 方法 B：打包成 .crx 自分发

```bash
# Chromium 自带打包功能（命令行）
chromium --pack-extension=./web/extension/get-cookies
# 或在 chrome://extensions/ 页面用 "打包扩展程序" 按钮
```

打包出的 `.crx` 拖入扩展页即可安装。**不要**直接复用上游 manifest 里的 `key` 字段（已移除），否则 Chrome 拒绝安装。

## 工作原理

```
┌────────────────────┐                    ┌──────────────────┐
│ QD 页面 (allowlist  │ ◄── postMessage ── │ 内容脚本          │
│  内的 origin)       │                    │ (注入到 QD 页)    │
│                    │                    └────────┬─────────┘
│  按下 [获取] 按钮  │                             │ chrome.runtime
│  (data-toggle=     │                             │ .sendMessage
│   get-cookie       │                             ▼
│   data-site=...)   │                    ┌──────────────────┐
└──────────────────┬─┘                    │ service_worker   │
                   │                      │  chrome.cookies  │
                   └─ click 事件 ─────────►  按 site 取所有  │
                                          │  cookie 返回     │
                                          └──────────────────┘
```

消息协议：

| 方向 | info | data |
| --- | --- | --- |
| 内容脚本 → QD | `get-cookieModReady` | — |
| QD 按钮 click → 内容脚本 | DOM 事件，读 `data-site` 属性 | — |
| 内容脚本 → service_worker | `{ do: 'get_cookie', site }` | — |
| service_worker → 内容脚本 | `{ cookies: { name: value, ... } }` | — |
| 内容脚本 → QD | `cookieRaw` | `{ name: value, ... }` |

## 与上游 [qd-today/get-cookies](https://github.com/qd-today/get-cookies) 的差异

| 改动 | 原因 |
| --- | --- |
| Manifest V3 + 标准 `chrome.runtime.onMessage`（去掉 long-lived port） | 简化代码，更符合 MV3 推荐写法 |
| 站点白名单按 `URL.hostname` 精确 + 后缀匹配，不再用 `String.includes` | 防止 `evil-qiandao.today` 被当作 `qiandao.today` 命中 |
| `postMessage` 目标 origin 改为 `window.location.origin`，不再用 `'*'` | QD 前端本来就只接收 `event.origin === window.location.origin` 的消息，原行为是冗余且潜在风险 |
| 注入脚本只在 `tab.status === 'complete'` 且顶层 frame（`frameIds: [0]`）触发 | 避免 SPA 路由切换时重复注入；避免在 iframe 内运行 |
| 内容脚本加重入保护（`window.__qdGetCookieInjected`） | 避免 chrome.scripting 多次注入时重复绑定监听 |
| Options 默认值改为空字符串（不再硬编码 `192.168.0.111`），保存时校验 URL 合法性 | 减少误用 |
| 移除 manifest 里的硬编码 `key` 字段 | 该字段是上游 Chrome Web Store 发布用的固定 ID，自分发时复用会导致安装失败 |
| 增加英文 i18n（`_locales/en/`） | 国际化 |
| 头部注释、JSDoc、严格模式 `'use strict'` | 代码质量 |

## 不想用扩展？

如果只是临时用一下，又不想装扩展，可以用 **bookmarklet**（书签小工具）——把以下代码做成书签，登录目标站点后点一下即可把可见 cookie 复制到剪贴板：

```javascript
javascript:(()=>{const c=document.cookie;if(!c){alert('未获取到 cookie，请确认你已登录');return;}navigator.clipboard.writeText(c).then(()=>alert('已复制 '+c.split(';').length+' 项 cookie 到剪贴板'));})();
```

⚠️ Bookmarklet 受 `document.cookie` 限制，**拿不到 HttpOnly cookie**（很多签到站的 session cookie 都是 HttpOnly）。完整支持仍需安装本扩展。

## 许可

MIT，见 [LICENSE](./LICENSE)。基于上游 `qd-today/get-cookies`（同样为 MIT 风格分发）二次创作。
