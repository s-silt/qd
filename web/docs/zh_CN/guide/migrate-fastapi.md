# 从 Tornado 迁移到 FastAPI

> QD 自 v20260429 起 **默认 Web 层为 FastAPI**（uvicorn）。Tornado 旧版完整保留，可一键回退。本指南面向已经在生产运行老版本 QD 的用户，介绍**安全无损**完成切换的步骤。

---

## 一、TL;DR

如果你的部署满足：

- 用 Docker / docker compose
- `AES_KEY` 已经设过自定义值（**或者**一直用默认 `binux`）
- 不需要保留任何运行时调优过的内存状态

那么迁移=**`git pull` + `docker compose up -d --build` + 完事**。

```bash
cd qd
git pull
docker compose -f docker-compose.local.yml up -d --build qd
docker compose -f docker-compose.local.yml logs -f qd   # 观察启动日志
```

老用户不需要重新登录，老 HAR 模板继续按原计划重放，定时任务到点照常触发。

---

## 二、本次改动概览

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| **HAR 重放引擎** (`libs/fetcher.py`) | ❌ 未改 | 任务执行路径与之前完全一致 |
| **变量沙箱** (`libs/safe_eval.py`) | ❌ 未改 | 模板里 `{{ md5(time.time()) }}` 等表达式继续工作 |
| **Cookie 会话** (`libs/cookie_utils.py`) | ❌ 未改 | 站点登录态保持机制不变 |
| **数据库 schema** (`db/`) | ❌ 未改 | 字段、表结构、索引一致；只升级了 ORM 调用语法到 SQLAlchemy 2.0 |
| **Worker 调度** (`worker.py`) | ✅ 仅性能优化 | `push_batch` 修复 N+1，行为相同；`do()` 增加 user 不存在的守卫 |
| **Web 层路由 / Cookie 写入** | 🔁 切换框架 | Tornado → FastAPI；secure cookie 字节级兼容 |
| **登录态加密** (`AES_KEY`) | ❌ 未改 | 旧 cookie 用旧 key 解密，**不要换 key** |
| **新增功能** | ➕ AI 签到识别 / URL 自动抓包 / Go sidecar | 默认关闭，配环境变量启用 |

**关键点：迁移是 Web 层换框架，不是数据迁移**。worker / DB / 模板 都不动。

---

## 三、Cookie 兼容性

### 3.1 secure cookie 二进制级兼容

新版 `web/fastapi/auth.py` **完全实现** Tornado v2 secure cookie 格式：

```
2|<key_version>|<timestamp>|<name>|<base64(value)>|<hmac-sha256-hex>
```

- HMAC 算法、签名范围、字段顺序、url-safe base64 与 Tornado 一致
- 用 `hmac.compare_digest` 防时序攻击
- `httponly=True` / `samesite=lax` 保留
- `cookie_secure_mode` 仍然走 HTTPS-only 设置

**实测验证**：Tornado 写的 cookie 能被 FastAPI 读，反之亦然（覆盖 22 个 round-trip 单元测试）。

### 3.2 实操结论

- ✅ 老用户**不需要重新登录**
- ✅ Tornado 和 FastAPI **可以同时跑**（端口分别），用户在哪个端口登的就拿哪个端口的 cookie，互通
- ✅ 切回 Tornado 不丢登录态

### 3.3 唯一会让登录态失效的操作

**改 `COOKIE_SECRET`**。和框架切换无关——你换 key，所有用户都得重登。如果你之前没改默认 `binux`，建议**这次也别改**，等下次有用户教育机会再换。

---

## 四、加密数据兼容性（⚠️ 重要）

### 4.1 不要动 `AES_KEY`

数据库里这些字段是 AES 加密的：

- `task.init_env`、`task.env`、`task.session`（任务的 cookie / token / 自定义变量）
- `tpl.har`、`tpl.tpl`（私有模板的 HAR 与渲染体）

它们用 `AES_KEY` 派生的密钥加密。**换 key 后无法解密 → 任务到点跑会因为读不出 cookie 报错**。

### 4.2 处理建议

| 场景 | 操作 |
| --- | --- |
| 一直用默认 `AES_KEY=binux` | 沿用 `binux`，**不要趁这次升级换** |
| 已经改过自定义值 | **填入完全一样的值** |
| 想换 key | 用 `backup.py` 解密导出全部数据 → 改 key 启动 → 重新导入 |

启动时如果检测到 `AES_KEY=binux`，控制台会输出 WARNING（这是新加的安全告警，仅警告不阻止启动）：

```
[安全] AES_KEY 未设置, 当前为默认值 'binux'。
       已存储的加密数据可被任何人解密, 建议生产环境覆盖该变量。
```

---

## 五、迁移步骤（Docker Compose）

### Step 0：备份（5 秒，强烈建议）

```bash
cd qd
tar -czf qd-pre-migration-$(date +%F).tar.gz config redis/data
```

`config/` 里是 SQLite 数据库 + 上传文件。如果用 MySQL 还要 `mysqldump`。

### Step 1：拉新代码

```bash
git pull
```

如果 `git pull` 提示有冲突（你本地改过 `docker-compose.local.yml`），优先保留你的修改：

```bash
git stash               # 暂存本地改动
git pull
git stash pop           # 恢复本地改动
```

### Step 2：保留密钥 / 域名设置

打开 `docker-compose.local.yml`，确认下面三项**与你之前生产值一致**：

```yaml
environment:
  - DOMAIN=qd.yourdomain.com           # 你的域名/IP
  - COOKIE_SECRET=<和之前完全相同>
  - AES_KEY=<和之前完全相同>
```

### Step 3：重建并启动

```bash
docker compose -f docker-compose.local.yml up -d --build qd
```

只重建 `qd` 服务即可，Redis / Playwright sidecar 等不动。首次构建大约 1-3 分钟（拉镜像 + pip install 新增的 fastapi/uvicorn）。

### Step 4：观察启动日志

```bash
docker compose -f docker-compose.local.yml logs -f qd
```

成功启动会看到：

```
[I QD.FastAPI fastapi_app:215] FastAPI: registered 18 router(s)
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:80
```

如果出现下列**之一**任意一行 WARNING，按 [四、4.1](#41-不要动-aes_key) 处理：

```
[安全] COOKIE_SECRET 未设置, 当前为默认值 'binux'
[安全] AES_KEY 未设置, 当前为默认值 'binux'
```

按 `Ctrl-C` 退出 logs（容器仍在跑）。

### Step 5：验证

```bash
# 1. 主页能开
curl -o /dev/null -w "%{http_code}\n" http://你的IP:8923/
# 应输出 200

# 2. 老 cookie 能直接进 /my/
curl -b "user=<你浏览器现有的 user cookie>" \
     -o /dev/null -w "%{http_code}\n" \
     http://你的IP:8923/my/
# 应输出 200, 不是 401

# 3. 看一个老任务到点能否执行
docker compose -f docker-compose.local.yml exec qd \
    tail -f /usr/src/app/config/runner.log
```

最稳的验证还是**等到下一个签到任务自然触发**，看 `tasklog` 里有没有正常 success。

---

## 六、回退到 Tornado

如果发现任何问题，**不需要 docker rebuild**，加一行环境变量重启即可：

```yaml
# docker-compose.local.yml
services:
  qd:
    environment:
      - WEB_FRAMEWORK=tornado    # ← 加这一行
      # ... 其他不变 ...
```

```bash
docker compose -f docker-compose.local.yml restart qd
```

容器启动时 `run.py` 检测到 `WEB_FRAMEWORK=tornado` 走老 Tornado 启动器，行为与升级前完全一致。**worker / 数据 / cookie / 任务 都不动**，回退是一次性的、原子的、零数据丢失。

---

## 七、迁移后的新能力（可选启用）

升级后这些功能默认关闭，按需开：

### 7.1 AI 智能识别签到

抓包后让 AI 自动识别签到接口、剔除噪声请求。

```yaml
environment:
  - AI_API_KEY=sk-xxxxxxxx
  - AI_BASE_URL=https://api.deepseek.com/v1   # 可选
  - AI_MODEL=deepseek-chat                     # 可选
```

详见 [`ai-sign-template.md`](./ai-sign-template.md)。

### 7.2 URL 自动抓包（Playwright sidecar）

给 URL + Cookie，自动启浏览器找签到按钮、点击、录 HAR。

启用步骤见 [`auto-capture.md`](./auto-capture.md)。

### 7.3 Go 版 sidecar（轻量）

Python sidecar 1.5GB；Go 版仅 250MB。在 `docker-compose.local.yml` 里把 Python 版的 `playwright:` 段注释掉，启用 `playwright-go:` 段即可。

---

## 八、风险与已知差异

### 8.1 worker 启动机制变了

老版（Tornado）：`PeriodicCallback` 调度 `BatchWorker`，`add_callback(QueueWorker())`。

新版（FastAPI lifespan）：

```python
# QueueWorker
asyncio.create_task(worker())

# BatchWorker - 等价于 PeriodicCallback 的 asyncio 版
async def loop():
    while True:
        await worker()
        await asyncio.sleep(config.check_task_loop / 1000)
```

调度间隔、worker 行为、错误处理 **完全一致**，只是底层从 Tornado IOLoop 换成了 asyncio。`worker.py` 文件本身没动一行。

### 8.2 WebSocket 部分

`SubscribeUpdatingHandler` 的实时更新流（订阅公共模板时拉取最新版本的 WebSocket 通道）目前在 FastAPI 版只移植了**握手骨架**，完整的拉取/广播逻辑标记为 TODO。**不影响任务执行 / 模板使用 / 签到流程**——只影响"在公共模板列表里看更新进度条"这种少数管理员场景。如果你强依赖该功能，先用 `WEB_FRAMEWORK=tornado` 跑。

### 8.3 端口

| 启动方式 | 端口（默认） | 控制方式 |
| --- | --- | --- |
| `python run.py`（默认 FastAPI） | `config.port`（8923） | `PORT` 环境变量 |
| `python run_fastapi.py` | `FASTAPI_PORT` 或 `config.port` | `FASTAPI_PORT` 环境变量 |
| Docker compose | 容器内 80（映射 8923→80） | 不动 docker-compose 即可 |

### 8.4 调试

FastAPI 异常默认会写 traceback 到 stderr。生产环境如果不想暴露，确认 `QD_DEBUG=False`（默认就是）。

---

## 九、常见问题

### Q1：升级后浏览器一直跳 `/login`

→ `COOKIE_SECRET` 被改了。检查 `docker-compose.local.yml` 里的值，恢复到升级前的值。

### Q2：任务到点报错 `decrypt failed` 或 `Invalid encrypted data`

→ `AES_KEY` 被改了。把它恢复到升级前的值，重启容器。

### Q3：`docker compose up` 卡在 `Building qd` 拉 pip 包很慢

→ 国内网络问题。在 `Dockerfile.local` 里加 pip 镜像：
```dockerfile
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    --no-cache-dir -r requirements.txt --break-system-packages
```

### Q4：日志一直刷 `connect to redis: refused`

→ Redis 容器没起。`docker compose -f docker-compose.local.yml ps` 看 redis 状态；`docker compose restart qd` 等 redis 起来后再启 qd。

### Q5：怎么确认在跑哪个框架？

```bash
docker compose -f docker-compose.local.yml logs qd | grep -E "FastAPI|Tornado"
# FastAPI: "registered 18 router(s)"
# Tornado: "Http Server started on ..."
```

或者 `curl -I http://你的IP:8923/`，FastAPI 的 `Server: uvicorn`，Tornado 的 `Server: TornadoServer/...`。

---

## 十、下一步

- [HAR 抓包教程](./har-capture.md)
- [AI 自动生成签到模板](./ai-sign-template.md)
- [URL 自动抓包](./auto-capture.md)
- [Docker 部署完整教程](./docker-deploy.md)
- [CHANGELOG](../../../../CHANGELOG.md)（看完整本次改动清单）
