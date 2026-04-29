# QD 框架前端重设计评估报告

**日期：** 2026-04-29  
**评估范围：** `web/tpl/` 全部模板、`web/static/css/` 自有样式、`web/static/components/` 第三方库  
**受众：** 项目维护者及 stakeholder（无需技术背景即可阅读）

---

## 1. 现状摘要

### 技术栈

| 层次 | 技术 | 版本 | 说明 |
|---|---|---|---|
| CSS 框架 | Bootstrap | **3.2.0** | 2014 年发布，已停止维护 |
| JS 框架 | AngularJS | 1.x | 仅用于 HAR 编辑器（`har/editor.html`） |
| 模块加载 | Sea.js | — | 老式 CMD 模块系统，现代浏览器已原生支持 ES Module |
| 模板引擎 | Jinja2 | — | 服务端渲染，无构建工具 |
| 其他 | jQuery + Select2 + tablesorter + nprogress + Font Awesome | — | 全部 CDN-free 本地化 |

### 页面分类

**纯服务端渲染（SSR）页面**：`login.html` / `register.html` / `index.html` / `my.html` / `task_new.html` / `task_setTime.html` / 全部管理页。这些页面只用 jQuery 做少量 DOM 操作，无 SPA 行为，HTML 结构稳定。

**含 SPA 行为的页面**：`har/editor.html` — AngularJS `ng-controller="EntryList"` 控制器，使用 `ng-repeat`、`ng-class`、`ng-switch`、双向绑定、自定义 filter 等大量 AngularJS 指令；同时依赖 `angular.min.js`、`js-base64`、`draggable-polyfill` 等。该页面与业务逻辑深度耦合，改动风险最高。

### 视觉痛点

1. **Bootstrap 3 的 3D 渐变按钮**：`.btn-default` 是明显的立体感设计，与现代扁平趋势脱节
2. **背景图片**：`body { background: url(../img/body.jpg) repeat-x }` — 通栏条纹背景感觉过时
3. **`viewport` 设置**：`user-scalable=1` 不是标准写法，移动端缩放体验差
4. **间距紧凑**：Bootstrap 3 的默认间距偏小，在高分屏上显得拥挤
5. **无暗色模式**：CSS 中没有任何 `prefers-color-scheme` 查询
6. **`my.html` 的表格**：任务数量多时横向溢出，移动端不可用

---

## 2. 三套方案对比

### 方案 A — CSS-only 美化

**思路：** 不动 HTML 结构与 Jinja2 模板，只重写或叠加 `base.css` / `index.css` / `my.css` / `register.css` 等自有 CSS 文件，利用 CSS 变量覆盖 Bootstrap 3 默认样式，加入圆角、柔和阴影、扁平按钮。

| 维度 | 评估 |
|---|---|
| **工作量** | 1–2 周，单名前端可独立完成 |
| **视觉提升** | 约 60%：按钮、卡片、表单从"2014 Bootstrap 默认"升级为现代扁平风 |
| **兼容性** | 完美：完全不动 AngularJS 控制器、jQuery 逻辑和 Jinja2 模板 |
| **移动端** | 受限于 Bootstrap 3 原有 grid，可改善 `viewport` meta 和字号，但表格溢出问题无法根治 |
| **逐步切换路径** | 新增一个 `theme-flat.css`，在 `base.html` 末尾 `<link>` 引入即可切换；回退只需移除这一行 |

**前提条件：** CSS 变量（`var(--xxx)`）需要目标浏览器支持（Chrome 49+, Firefox 31+, Safari 10+，IE 不支持）。  
**主要难点：** Bootstrap 3 大量使用 `!important` 和高特异性选择器，覆盖时需要同等或更高的特异性，或在 `<link>` 顺序上位于 bootstrap 之后。

---

### 方案 B — 组件化重构（Tailwind CSS）

**思路：** 引入 Tailwind CSS（CDN Play CDN 或本地 PostCSS build），移除 Bootstrap 3，重写所有 `.html` 模板中的 `class="btn btn-default"` 等 class 为 Tailwind utility class，保留 AngularJS 和 jQuery 逻辑不动。

| 维度 | 评估 |
|---|---|
| **工作量** | 4–6 周；38 个模板、大量 `btn-*` / `form-control` / `table` class 全部需要逐一替换 |
| **视觉提升** | 约 90%：完全现代化，可获得一致的 design token，维护性大幅提升 |
| **兼容性** | 中等风险：`utils.html` 中动态插入的 HTML（`$('#modal_load .modal-content').html(data)`）使用 Bootstrap class，若移除 Bootstrap 这些弹窗内容样式会破碎；需要同步改造 modal 逻辑 |
| **移动端** | Tailwind 响应式前缀（`md:` `lg:`）可从根本上解决表格溢出问题 |
| **逐步切换路径** | 可先保留 Bootstrap CDN 做兜底，逐页替换 class，测试通过后再移除 Bootstrap；但 Tailwind + Bootstrap 同时存在会产生样式冲突，过渡期需要维护隔离命名空间 |

**前提条件：** 需要引入 Node.js / PostCSS 构建流程（若用 Play CDN 则不需要，但生产包会变大）。  
**主要难点：** AngularJS 模板用 `{% raw %} ... {% endraw %}` 包裹，其中的 `{{ expr }}` 语法与 Jinja2 冲突；这部分 class 替换需要格外小心，不能破坏 `ng-class` 绑定。

---

### 方案 C — 整站重写（Vue / React + 现代 UI 库）

**思路：** 用 Vite + Vue 3（或 React 18）+ Naive UI / Ant Design Vue 写一套全新前端，通过 QD 后端已有的 HTTP API 获取数据，原 Jinja2 模板作为兜底保留。

| 维度 | 评估 |
|---|---|
| **工作量** | 3–6 个月；需要梳理所有 API 接口、实现权限/会话管理、复刻 HAR 编辑器的 AngularJS 逻辑 |
| **视觉提升** | 100%：完全现代化，可获得移动端原生体验、SSR/SSG 支持 |
| **兼容性** | 高风险：后端 `modal_load()` 等模式依赖服务端返回 HTML 片段，整站重写需要改造为 JSON API |
| **移动端** | 从零设计，可做到完美响应式 |
| **逐步切换路径** | 可以"路由级"灰度：先替换 `/login` 和 `/my`，再逐步替换其他路由，旧 Jinja2 路由保留做降级 |

**前提条件：** 需要后端配合暴露 RESTful JSON API（目前许多端点返回 HTML 片段），工作量不只在前端。  
**主要难点：** HAR 编辑器是整个项目最复杂的 UI，AngularJS 版本有几百行控制器代码；迁移到 Vue/React 需要完整测试，稍有疏漏就会破坏核心的 HTTP 录制功能。

---

## 3. 推荐路径

### 第一步（现在）：方案 A

**推荐先做方案 A**，理由如下：

1. **零业务风险**：不碰任何 AngularJS 控制器、jQuery 逻辑、Jinja2 模板，线上回退只需删除一行 `<link>`
2. **即时可见收益**：登录页、任务列表、我的页面这三个最高频页面可在 1–2 周内完成改版
3. **为方案 B/C 打底**：方案 A 产出的 CSS 变量体系（`--color-primary`、`--border-radius` 等）可以直接被方案 B 的 Tailwind 配置或方案 C 的 UI 库 token 复用

### 第二步（3–6 个月后）：评估方案 B

在方案 A 上线、收集用户反馈后，若项目引入了 Node.js 构建流程，可以逐模板迁移 Tailwind，进一步提升维护性。

### 不急于方案 C

整站重写的前提是后端 API 的系统性整理，建议等后端 FastAPI 迁移（正在并行进行）稳定后再评估。

---

## 4. POC 截图说明

本次已实现方案 A 的可视化 POC，文件全部放在 `web/static/css/redesign/`，**直接用浏览器打开即可预览，无需服务器**。

### 文件清单

| 文件 | 说明 |
|---|---|
| `web/static/css/redesign/theme-flat.css` | 主题核心：CSS 变量 + 扁平按钮、卡片、表单、徽章、表格、空状态等 18 个组件节 |
| `web/static/css/redesign/theme-flat-dark.css` | 暗色模式叠加层，支持 `prefers-color-scheme: dark` 和 `.dark` class 两种激活方式 |
| `web/static/css/redesign/demo-index.html` | **任务列表页** POC：统计条、工具栏（批量操作 + 搜索）、带分组的扁平表格（5 种状态徽章）、4 列响应式卡片视图、空状态插图、暗色切换按钮 |
| `web/static/css/redesign/demo-login.html` | **登录/注册页** POC：登录/注册 tab 切换、密码可见性切换、密码强度条、忘记密码子面板、≥900px 时显示功能介绍侧栏、CSS 装饰背景 |

### 预期视觉效果

**demo-index.html：**
- 顶部粘性导航栏（白底 + 绿色品牌 Logo）
- 4 格统计卡片：总任务 / 运行中 / 已禁用 / 今日成功
- 任务表格：带绿色分组表头行、行悬停高亮、成功（绿）/ 失败（红）/ 待重试（橙）/ 执行中（蓝脉冲）/ 已禁用（灰）5 种徽章
- 卡片视图：圆角卡片网格，最后一格为"添加任务"虚线卡片
- 移动端 < 768px 自动切换为单列
- 右上角点击 🌙 切换暗色模式（背景变深蓝灰，文字自动调整）

**demo-login.html：**
- 居中认证卡片，浅绿 + 灰色渐变背景（纯 CSS，无图片）
- ≥ 900px 时左侧显示产品功能介绍（含图标列表）
- 登录 / 注册 tab 切换，带底部指示线动效
- 密码输入框右侧眼睛图标切换明文 / 密文
- 注册时输入密码实时显示强度条（弱 / 一般 / 较强 / 强）
- 登录提交后模拟 1.2s 加载，显示错误 alert（红色左边框）

---

## 5. 不在本次范围内

以下主题超出本次评估范围，建议后续单独立项：

- **移动 App**：若需要原生 iOS / Android 体验，需要独立 App 项目或 PWA 封装，与本次 Web 重设计是两条不同的轨道
- **国际化（i18n）**：QD 目前全中文硬编码；支持英语等多语言需要在模板层引入 i18n 机制（如 Flask-Babel 或前端 vue-i18n），工作量独立于视觉改版
- **无障碍（a11y）**：现有模板缺少 `aria-*` 属性、`<label for>` 关联、键盘焦点管理；完整 WCAG 2.1 AA 合规需要专项审计
- **性能优化**：静态资源无 cache-busting 策略（目前用 Sea.js map 加版本号，但不规范）；可考虑引入 HTTP/2 + CDN + 资源压缩，但这属于基础设施层面
- **单元测试 / E2E 测试**：前端目前无任何自动化测试；引入 Playwright 或 Cypress 是独立工作
- **安全加固**：CSP（Content Security Policy）头的设置需要与后端协作，不属于纯前端重设计范畴

---

*本报告由 Claude Code 自动生成，基于对 QD 仓库 `web/` 目录的静态分析，日期 2026-04-29。*
