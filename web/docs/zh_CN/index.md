---
layout: home
title: HTTP定时任务自动执行框架

hero:
  name: QD
  text: 一个 HTTP 定时任务自动执行 Web 框架
  tagline: ""
  image:
    src: /logo.png
    alt: QD
  actions:
    - theme: brand
      text: 开始了解
      link: /zh_CN/guide/what-is-qd
    - theme: alt
      text: 在 GitHub 中查看
      link: https://github.com/s-silt/qd

features:
  - title: 基于Har
    details: 仅需上传通过抓包得到的 Har, 即可制作框架所需的 HTTP 任务模板。
  - title: AI 智能识别
    details: 一键调用大模型（OpenAI/DeepSeek/通义/本地 Ollama），自动从 HAR 中识别签到接口并生成模板。
  - title: URL 自动抓包
    details: 给 URL + Cookie，QD 自动用 Playwright 加载页面、找签到按钮、录制 HAR 并生成模板。
  - title: Tornado 服务端
    details: 使用 Tornado 作为服务端, 以实现异步响应前端请求和发起 HTTP 请求。
  - title: API & 插件支持
    details: 内置多种 API 和过滤器用于模板制作, 后续将提供自定义插件支持。
  - title: 开源项目
    details: QD 是一个基于 MIT 许可证的开源项目。
---
