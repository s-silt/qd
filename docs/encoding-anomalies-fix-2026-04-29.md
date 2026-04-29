# 编码异常字符修复报告 2026-04-29

## 概要

| 项目 | 值 |
|------|---|
| 发现日期 | 2026-04-29 |
| 修复提交分支 | `claude/fix-encoding-anomalies` |
| 修复 U+FFFD | 49 处（跨 2 个文件：1 处在 ai_client.py，48 处在 security-audit 报告） |
| 修复 U+200B | 0 处（上游历史遗留，不在本次修复范围） |
| 预防机制 | pre-commit hook + GitHub Actions CI |

---

## 一、发现的问题清单

### 1.1 `libs/ai_client.py:100` — 1 处 U+FFFD

| 字段 | 内容 |
|------|------|
| 文件 | `libs/ai_client.py` |
| 行号 | 100 |
| 原文 | `"AI 响���结构不符合预期: %.500s"` |
| 修复 | `"AI 响应结构不符合预期: %.500s"` |
| 推断原字符 | `应`（U+5E94，UTF-8 字节 `E5 BA 94`，3 字节均被替换为 U+FFFD） |

### 1.2 `docs/security-audit-2026-04-29.md` — 48 处 U+FFFD（35 行）

| 行号 | 原文（含 FFFD） | 修复为 | 原字符推断 |
|------|----------------|--------|-----------|
| 5 | `\| ���度 \| 值 \|` | `\| 严重度 \| 值 \|` | 严 |
| 26 | `触发��误` | `触发错误` | 错 |
| 26 | `JSON error ���段` | `JSON error 字段` | 字 |
| 30 | `同样���\`AI` | `同样于\`AI` | 于 |
| 43 | `响��中将` | `响应中将` | 应 |
| 69 | `生产��署` | `生产部署` | 部 |
| 69 | `署���未显式` | `署未显式` | （多余 FFFD，删除） |
| 69 | `显式��用` | `显式启用` | 启 |
| 70 | `生产环境开��` | `生产环境开启` | 启 |
| 79 | `为��时` | `为空时` | 空 |
| 84 | `验证���增加` | `验证中增加` | 中 |
| 84 | `默��拒绝` | `默认拒绝` | 认 |
| 93 | `模型��可泄露` | `模型名可泄露` | 名 |
| 114 | `密码存��路径` | `密码存储路径` | 储 |
| 114 | `路径���\`db/` | `路径（\`db/` | （ |
| 125 | `可接��` | `可接受` | 受 |
| 125 | `出现��内存` | `出现于内存` | 于 |
| 125 | `运���配置` | `运维配置` | 维 |
| 145 | `` `False`��fail-open`` | `` `False`（fail-open`` | （ |
| 145 | `下���力破解` | `下暴力破解` | 暴 |
| 153 | `字节码��名单` | `字节码白名单` | 白 |
| 153 | `两套独���机制` | `两套独立机制` | 立 |
| 168 | `cookie ��已脱敏` | `cookie 名已脱敏` | 名 |
| 172 | `仅��集` | `仅收集` | 收 |
| 177 | `（���计选择` | `（设计选择` | 设 |
| 181 | `\| ��全局 XSRF` | `\| 无全局 XSRF` | 无 |
| 181 | `同样�� CSRF` | `同样无 CSRF` | 无 |
| 181 | `一致���择` | `一致选择` | 选 |
| 185 | `不用于���部密码` | `不用于内部密码` | 内 |
| 189 | `落地���汇总` | `落地项汇总` | 项 |
| 191 | `变���摘要` | `变更摘要` | 更 |
| 193 | `原始������降级` | `原始日志降级` | 日志 |
| 195 | `None���纯 dots` | `None、纯 dots` | 、 |
| 200 | `记��原因` | `记录原因` | 录 |
| 207 | `业务��达式` | `业务表达式` | 表 |
| 209 | `从接��中移除` | `从接口中移除` | 口 |
| 213 | `下一步���按` | `下一步（按` | （ |
| 215 | `防��匿名` | `防止匿名` | 止 |
| 215 | `风险��低` | `风险极低` | 极 |
| 216 | `部署��档中` | `部署文档中` | 文 |
| 217 | `加隐��提示` | `加隐式提示` | 式 |
| 219 | `增加��地内存` | `增加本地内存` | 本 |
| 230 | `格式���误` | `格式错误` | 错 |
| 230 | `错误���一返回` | `错误统一返回` | 统 |
| 231 | `错误仅��录` | `错误仅记录` | 记 |
| 233 | `现有��知` | `现有已知` | 已 |
| 234 | `不�� regression` | `不是 regression` | 是 |
| 235 | `5 天）���与` | `5 天），与` | ， |

### 1.3 `web/docs/guide/how-to-use.md:28` 和 `web/docs/zh_CN/guide/how-to-use.md:30` — U+200B（零宽空格）

**不修复**。经 `git log --follow` 确认，这两处 U+200B 来自上游 bot commit：

```
71733ac  docs: add Siman8 as a contributor for code (#483)
Author:  allcontributors[bot]
Date:    2023-11-12
```

这是 allcontributors 机器人在 2023 年写入的历史文件，不在本次维护范围内，列为已知遗留项。

### 1.4 第三方压缩文件

`web/static/components/` 下的 minified JS/CSS 含 U+FFFD（第三方库内部字符，非损坏），**不动**。

---

## 二、修复方式

### Phase 1 修复

1. **`libs/ai_client.py:100`**：直接 Edit 工具替换单个字符。
2. **`docs/security-audit-2026-04-29.md`**：编写 Python 脚本，通过精确字符串匹配逐一替换所有 FFFD 位置，替换完成后验证文件不含任何 U+FFFD。

验证命令（通过）：
```python
python -c "
import sys
for f in ['libs/ai_client.py', 'docs/security-audit-2026-04-29.md']:
    with open(f, encoding='utf-8') as fh: c = fh.read()
    assert '\\ufffd' not in c, f'{f} still has U+FFFD'
print('PASS')
"
```

---

## 三、预防机制

### 3.1 Pre-commit hook（本地拦截）

文件：`scripts/check-encoding.sh`

添加到 `.pre-commit-config.yaml`：
```yaml
- repo: local
  hooks:
    - id: check-encoding
      name: lint-encoding (U+FFFD / invisible chars)
      language: script
      entry: scripts/check-encoding.sh
      args: ["--staged"]
      pass_filenames: false
      always_run: true
```

每次 `git commit` 时自动扫描暂存文件，发现异常字符则输出 `file:line: issue` 并阻止提交。

### 3.2 GitHub Actions CI（远程兜底）

文件：`.github/workflows/check-encoding.yml`

每次向 `master`/`dev` push 或 PR 时，在 ubuntu-latest 跑一遍全仓库扫描。与 pre-commit hook 互补，防止绕过本地 hook 直接 push 的情况。

### 3.3 Makefile target

```bash
make lint-encoding   # 扫描整个仓库
```

---

## 四、本地手动检查用法

```bash
# 安装脚本权限（首次）
chmod +x scripts/check-encoding.sh

# 扫描整个仓库
./scripts/check-encoding.sh

# 只检查暂存文件（pre-commit 模式）
./scripts/check-encoding.sh --staged

# 检查指定文件
./scripts/check-encoding.sh libs/ai_client.py

# 检查指定目录
./scripts/check-encoding.sh docs/

# 通过 Makefile
make lint-encoding
```

检测到异常时输出格式：
```
path/to/file.py:42: U+FFFD (UTF-8 replacement character — corrupted bytes)
path/to/doc.md:7: U+200B (zero-width space)
[check-encoding] 发现 2 个文件含编码异常字符，请修复后再提交。
```

---

## 五、已知遗留项（不修复）

| 文件 | 字符 | 来源 | 原因 |
|------|------|------|------|
| `web/docs/guide/how-to-use.md:28` | U+200B | allcontributors[bot] commit 71733ac (2023-11-12) | 上游历史，非本项目引入 |
| `web/docs/zh_CN/guide/how-to-use.md:30` | U+200B | 同上 | 同上 |
| `web/static/components/**/*.min.js` | U+FFFD | 第三方库 | minified 第三方文件，不维护 |

---

## 六、常见原因分析

以下是 U+FFFD 进入代码/文档的典型场景：

### 原因 1：AI/LLM 在长上下文输出中文时 token 切边导致字节序列损坏

LLM（如 GPT、Claude）以 token 为单位生成文本。中文 UTF-8 字符通常占 3 字节，而 BPE token 边界可能恰好切在多字节序列中间。当生成结果被截断（如 `text[:500]`）或上下文窗口溢出时，尾部可能出现不完整的 UTF-8 字节。如果下游代码用 `errors='replace'` 解码，不完整字节被替换为 U+FFFD。

**本次事故即此原因**：security audit agent 在生成/写入文档时，部分中文字符的 UTF-8 字节序列在某个中间步骤被截断或丢失。

### 原因 2：编辑器配置成非 UTF-8 保存（GBK / Latin-1）

在 Windows 上，如果编辑器（如老版本 Notepad、VS Code 配置错误）以 GBK 或 ANSI 编码保存含中文的文件，Python/Git 用 UTF-8 读取时会解码失败，产生 U+FFFD。

**防御方**：在 `.editorconfig` 中统一配置 `charset = utf-8`；在 Git 属性中设置 `text eol=lf`。

### 原因 3：跨剪贴板复制粘贴时编码不一致

从 Windows 应用（如 Word、Excel、老版终端）复制中文文本粘贴到 Linux 编辑器时，剪贴板可能以 UTF-16 LE 传输，而目标程序以 UTF-8 解释，导致部分汉字出现 U+FFFD。

**防御方**：使用 `file -i <filename>` 验证编码；终端统一设置 `LANG=zh_CN.UTF-8`。

### 原因 4：SSH 终端 locale 不是 UTF-8 导致命令输出截断

SSH 连接时如果客户端或服务端的 locale 未设置为 UTF-8（如 `LANG=C` 或 `LANG=en_US`），`echo`/`cat` 等命令输出的中文可能被截断或替换。通过管道写入文件时会保存损坏字节。

**防御方**：服务器和客户端均设置 `export LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8`；SSH 配置 `SendEnv LANG LC_*`。

### 原因 5：某些 API 返回字符串重复编解码（double-encoding）

部分 API 客户端库对响应体进行了两次解码（如先以 Latin-1 解码再以 UTF-8 编码），中文字符会经历 Mojibake（乱码）流程。最终保存的字符串可能以 U+FFFD 代替原始中文。

**防御方**：明确指定 `response.encoding = 'utf-8'` 或使用 `response.content`（bytes）再手动解码；使用 `chardet`/`charset-normalizer` 库自动检测编码。

---

## 七、测试验证

```bash
# 1. 验证修复文件无 FFFD
python -c "
for f in ['libs/ai_client.py', 'docs/security-audit-2026-04-29.md']:
    with open(f, encoding='utf-8') as fh: c = fh.read()
    assert '�' not in c, f'{f} still has U+FFFD'
print('PASS')
"

# 2. 验证 hook 能检出 FFFD（tmp 文件测试）
python3 -c "
with open('/tmp/tmp_test.md', 'w') as f:
    f.write('# Test\n含损坏字符�在这\n')
"
bash scripts/check-encoding.sh /tmp/tmp_test.md  # 应输出 issue 并 exit 1

# 3. 现有测试套件
python -m pytest tests/ -q  # 204 passed（8 pre-existing failures 与本次无关，3 skipped）
```
