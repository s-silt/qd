# QD 开箱即用模板

本目录收录可直接导入 QD 的签到模板（QD 模板 JSON 数组格式）。

## 如何导入

1. 下载对应的 `.json` 文件。
2. 登录 QD → `我的模板` 右侧 `+` → 上传该文件。
   - 文件是 QD 模板数组（不带 `log` 字段），上传时会自动转换并带上断言 / 变量提取规则。
3. 在编辑器右侧变量面板填入所需变量（通常是 `cookie`），测试通过后保存。
4. 到 `我的任务` 新建定时任务，选择该模板，填入变量与 Crontab 执行时间。

## 模板列表

| 文件 | 站点 | 说明 | 文档 |
| --- | --- | --- | --- |
| [`nodeseek-signin.json`](./nodeseek-signin.json) | [NodeSeek](https://www.nodeseek.com) | 每日签到领鸡腿（`random=true` 随机 / `false` 固定 5 个） | [NodeSeek 每日自动签到](../web/docs/zh_CN/guide/nodeseek-checkin.md) |

> 需要的变量、Cloudflare 处理、定时设置等细节，请看每个模板对应的文档。
