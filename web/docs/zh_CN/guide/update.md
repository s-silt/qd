# 更新方法

> 操作前请务必备份数据库！
>
> 更新后请重启容器或清空浏览器缓存。

## Docker Compose 部署更新（推荐）

```bash
cd qd
git pull
docker compose -f docker-compose.local.yml up -d --build
```

首次构建大约 1-3 分钟；后续更新若无新依赖则走 Docker layer 缓存，几秒内完成。

## 备份与回滚

```bash
# 更新前备份（5 秒）
tar -czf qd-backup-$(date +%F).tar.gz config redis/data

# 回滚到指定版本
git log --oneline -10
git checkout <commit-sha>
docker compose -f docker-compose.local.yml up -d --build
```

## Web 框架切换

自 v20260429 起默认使用 FastAPI（uvicorn）。如需切换回 Tornado：

```yaml
# docker-compose.local.yml
environment:
  - WEB_FRAMEWORK=tornado
```

```bash
docker compose -f docker-compose.local.yml restart qd
```

详见 [Tornado → FastAPI 迁移指南](./migrate-fastapi.md)。

## 与上游同步

```bash
git remote add upstream https://github.com/qd-today/qd.git
git fetch upstream
git merge upstream/master
git push origin master
docker compose -f docker-compose.local.yml up -d --build
```
