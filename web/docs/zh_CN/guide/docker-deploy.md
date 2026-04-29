# Docker 部署教程（s-silt/qd fork）

> 本教程介绍如何使用 [s-silt/qd](https://github.com/s-silt/qd) 这个 fork 仓库部署 QD 框架。fork 包含 AI 智能识别签到、worker N+1 优化等本仓库新增功能。
>
> 官方 Dockerfile（`Dockerfile`）会在构建时从 `gitee.com:qd-today/qd` 拉取上游代码，**不会包含 fork 的修改**。所以本教程使用专为 fork 准备的 `Dockerfile.local` 与 `docker-compose.local.yml`，直接基于本地源码构建。

## 目录

- [一、环境准备](#一环境准备)
- [二、快速部署（推荐：docker compose）](#二快速部署推荐docker-compose)
- [三、纯 Docker 命令部署](#三纯-docker-命令部署)
- [四、生产环境配置](#四生产环境配置)
  - [必改：密钥与域名](#必改密钥与域名)
  - [使用 MySQL 替代 SQLite](#使用-mysql-替代-sqlite)
  - [启用 AI 辅助签到模板生成](#启用-ai-辅助签到模板生成)
  - [Nginx 反向代理 + HTTPS](#nginx-反向代理--https)
- [五、更新 / 回滚 / 备份](#五更新--回滚--备份)
- [六、常见问题](#六常见问题)

---

## 一、环境准备

### 系统要求

| 项目 | 最低 | 推荐 |
| --- | --- | --- |
| CPU | 1 核 | 2 核 |
| 内存 | 512 MB | 1 GB+ |
| 磁盘 | 2 GB | 10 GB+ |
| 系统 | 任意 Linux / macOS / Windows | Ubuntu 22.04+ / Debian 12+ |

### 安装 Docker

```bash
# Linux 一键脚本
curl -fsSL https://get.docker.com | bash
sudo systemctl enable --now docker

# 把当前用户加入 docker 组（可选，避免每次 sudo）
sudo usermod -aG docker $USER
newgrp docker

# 验证
docker --version
docker compose version   # 注意是 "docker compose"（V2），不是 docker-compose
```

> Windows / macOS 直接装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)。

---

## 二、快速部署（推荐：docker compose）

### 1. 克隆 fork 仓库

```bash
git clone https://github.com/s-silt/qd.git
cd qd
```

### 2. 修改 `docker-compose.local.yml`

至少改这三行（明文密钥不能用于生产）：

```bash
# 生成随机密钥
openssl rand -hex 32   # 用作 COOKIE_SECRET
openssl rand -hex 32   # 用作 AES_KEY
```

编辑 `docker-compose.local.yml`：

```yaml
environment:
  - DOMAIN=qd.yourdomain.com           # 你的域名（不带 http/https）或 IP:PORT
  - COOKIE_SECRET=刚才生成的第一个随机串
  - AES_KEY=刚才生成的第二个随机串
```

### 3. 启动

```bash
docker compose -f docker-compose.local.yml up -d --build
```

首次构建大约 1-3 分钟（视网络），看到 `qd Started` 表示成功。

### 4. 访问

打开浏览器访问 `http://你的服务器IP:8923` —— 第一个注册的账号会自动设为管理员。

### 5. 查看日志

```bash
docker compose -f docker-compose.local.yml logs -f qd
# Ctrl+C 退出查看
```

启动日志中如果看到下面这类 WARNING，说明你忘了改密钥：

```
[安全] COOKIE_SECRET 未设置, 当前为默认值 'binux'。
[安全] AES_KEY 未设置, 当前为默认值 'binux'。
```

---

## 三、纯 Docker 命令部署

不想用 compose 也行：

```bash
# 1. 拉取代码
git clone https://github.com/s-silt/qd.git && cd qd

# 2. 构建镜像
docker build -f Dockerfile.local -t s-silt/qd:local .

# 3. 启动 redis
docker run -d --name redis --restart unless-stopped \
    -v $(pwd)/redis/data:/data redis:alpine --loglevel warning

# 4. 启动 qd
docker run -d --name qd --restart unless-stopped \
    --link redis \
    -p 8923:80 \
    -v $(pwd)/config:/usr/src/app/config \
    -e DOMAIN=qd.yourdomain.com \
    -e COOKIE_SECRET=$(openssl rand -hex 32) \
    -e AES_KEY=$(openssl rand -hex 32) \
    -e REDISCLOUD_URL=redis://redis:6379 \
    s-silt/qd:local
```

---

## 四、生产环境配置

### 必改：密钥与域名

| 变量 | 说明 | 不改的后果 |
| --- | --- | --- |
| `COOKIE_SECRET` | 用户登录态加密密钥 | 任何人能伪造登录 |
| `AES_KEY` | 数据库内 har / 环境变量加密密钥 | 加密形同虚设 |
| `DOMAIN` | 邮件链接、推送链接显示域名 | 邮件重置密码、推送链接打不开 |

### 使用 MySQL 替代 SQLite

默认 SQLite 适合个人使用；任务量大或多用户建议用 MySQL。

在 `docker-compose.local.yml` 中加入 MySQL 服务：

```yaml
services:
  qd:
    # ... 已有内容 ...
    depends_on:
      - redis
      - mysql
    environment:
      - DB_TYPE=mysql
      - JAWSDB_MARIA_URL=mysql://qd:your-mysql-password@mysql:3306/qd?auth_plugin=mysql_native_password
      # ... 其他不变 ...

  mysql:
    image: mysql:8.0
    container_name: qd-mysql
    restart: unless-stopped
    environment:
      - MYSQL_ROOT_PASSWORD=root-password
      - MYSQL_DATABASE=qd
      - MYSQL_USER=qd
      - MYSQL_PASSWORD=your-mysql-password
    volumes:
      - ./mysql/data:/var/lib/mysql
    command:
      - --default-authentication-plugin=mysql_native_password
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
```

> 第一次启动 MySQL 后，QD 需要等 MySQL 完全就绪才能连接。如果 qd 容器先起来报连接错误，等 30 秒后 `docker compose restart qd`。

### 启用 AI 辅助签到模板生成

取消 `docker-compose.local.yml` 里 AI 段落的注释：

```yaml
environment:
  - AI_API_KEY=sk-xxxxxxxxxxxxxxxx          # 必填
  - AI_BASE_URL=https://api.deepseek.com/v1 # 可选，默认 https://api.openai.com/v1
  - AI_MODEL=deepseek-chat                  # 可选
```

然后 `docker compose -f docker-compose.local.yml up -d --force-recreate qd`。

详细操作流程见 [AI 转换签到模板教程](./ai-sign-template.md)。

### Nginx 反向代理 + HTTPS

直接暴露 8923 不安全。生产建议在前面挂 Nginx + Let's Encrypt：

```nginx
server {
    listen 443 ssl http2;
    server_name qd.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/qd.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/qd.yourdomain.com/privkey.pem;

    client_max_body_size 50m;   # HAR 文件最大 50MB

    location / {
        proxy_pass http://127.0.0.1:8923;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade   $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
}

server {
    listen 80;
    server_name qd.yourdomain.com;
    return 301 https://$host$request_uri;
}
```

注意启用 HTTPS 后建议同时设置 QD 环境变量：

```yaml
environment:
  - COOKIE_SECURE_MODE=True
  - MAIL_DOMAIN_HTTPS=True
```

---

## 五、更新 / 回滚 / 备份

### 更新代码（拉取 fork 最新提交并重新构建）

```bash
cd qd
git pull
docker compose -f docker-compose.local.yml up -d --build
```

### 回滚

```bash
git log --oneline -10              # 找到要回滚到的 commit
git checkout <commit-sha>
docker compose -f docker-compose.local.yml up -d --build
```

### 备份数据

```bash
# SQLite + 上传文件 + Redis（如启用持久化）
tar -czf qd-backup-$(date +%F).tar.gz config redis/data
# 如果用 MySQL
docker exec qd-mysql mysqldump -u qd -p qd | gzip > qd-mysql-$(date +%F).sql.gz
```

QD 自带的 `backup.py` 也可以做更细粒度备份，详见仓库 README。

### 与上游同步

如果想把上游 [qd-today/qd](https://github.com/qd-today/qd) 的新功能合并到 fork：

```bash
git remote add upstream https://github.com/qd-today/qd.git
git fetch upstream
git merge upstream/master
# 解决冲突后
git push origin master
```

---

## 六、常见问题

### Q1：构建时卡在 `pip install` 或下载非常慢

构建时网络受限。选其一：

1. 在 Dockerfile.local 里给 pip 加国内镜像：
   ```dockerfile
   RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
       -r requirements.txt --break-system-packages 2>/dev/null || true
   ```
2. 或者直接复用上游已编译镜像：把 `image: s-silt/qd:local` 改成 `image: qdtoday/qd:latest`，再用 volume 挂载本地代码：
   ```yaml
   services:
     qd:
       image: qdtoday/qd:latest
       # 删掉 build: 段
       volumes:
         - .:/usr/src/app
         - ./config:/usr/src/app/config
   ```
   注意这样新增的 Python 包不会自动安装，但本 fork 没有引入新依赖（`aiohttp` 上游镜像已自带），所以是安全的。

### Q2：浏览器打不开，端口冲突

`docker compose -f docker-compose.local.yml ps` 看 qd 是否 `Up`。
端口被占用就改 `8923:80` 左侧那个 8923 为别的端口。

### Q3：日志里一直报 `redis connection refused`

redis 容器没就绪就启动了 qd。`docker compose restart qd` 即可。

### Q4：怎么删除所有数据从头开始

```bash
docker compose -f docker-compose.local.yml down
rm -rf config redis/data mysql/data
docker compose -f docker-compose.local.yml up -d --build
```

### Q5：升级 Docker 镜像后老数据还在吗

在。所有用户数据都在宿主机的 `./config`（SQLite + 上传文件）和 `./mysql/data` / `./redis/data` 中，删容器重建不影响数据。

### Q6：怎么用更轻量的精简镜像

把 `Dockerfile.local` 第一行改成：

```dockerfile
FROM qdtoday/qd:lite-latest
```

精简版去掉了 ddddocr / opencv 等依赖，镜像体积小一半，但不能用 OCR 验证码识别功能。

---

## 七、相关文档

- [HAR 抓包教程](./har-capture.md)
- [AI 智能识别签到模板](./ai-sign-template.md)
- [使用指南](./how-to-use.md)
- [常见问题](./faq.md)
