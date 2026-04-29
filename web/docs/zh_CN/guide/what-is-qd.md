# 什么是 QD?

QD 是一个基于 HAR 编辑器的 HTTP 定时任务自动执行 Web 框架。自 v20260429 起，默认使用 **FastAPI / uvicorn** 作为 Web 层（原 Tornado 服务端完整保留，可一键切换）。

<!-- ![login](/login.png)
![index](/index.png) -->

## 特性

- **基于 HAR**: 仅需上传通过抓包得到的 HAR，即可制作框架所需的 HTTP 任务模板。
- **FastAPI 服务端**: 使用 FastAPI（uvicorn）作为 Web 服务端，实现异步响应；原 Tornado 服务端保留，通过 `WEB_FRAMEWORK=tornado` 环境变量可随时切换。
- **AI 智能识别签到**: 配置 `AI_API_KEY` 后，HAR 编辑器内置 AI 分析按钮，自动从抓包中提取签到接口。
- **URL 自动抓包**: 配合 Playwright sidecar，输入 URL + Cookie 即可自动录制 HAR 并生成模板。
- **API & 插件支持**: 内置多种 API 和过滤器用于模板制作。
- **开源**: QD 是一个基于 MIT 许可证的开源项目。

## 如何部署

请参考: [部署](deployment)

## 如何使用

请参考: [如何使用](how-to-use)

## 如何更新

请参考: [更新](update)

## 讨论

- Github: [问题反馈](https://github.com/qd-today/qd/issues)
- Github: [讨论](https://github.com/qd-today/qd/discussions)
