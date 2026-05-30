# Playwright sidecar 镜像（已废弃 / DEPRECATED）
# 「URL 自动抓包」现已集成进 QD 主镜像（进程内置 Playwright, 见 libs/playwright_capture.py）,
# 本 sidecar 已从 docker-compose 移除、不再随主镜像构建, 仅作历史保留。
# 如确需独立容器运行, 可手动 `docker build` 本目录（其 requirements.txt 已含 uvicorn/fastapi）。

FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

LABEL org.opencontainers.image.source="https://github.com/s-silt/qd"
LABEL org.opencontainers.image.description="QD Playwright sidecar for URL → HAR auto-capture"

WORKDIR /app

# 安装中文字体, 否则页面渲染会出现方块
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk fonts-noto-color-emoji tzdata && \
    rm -rf /var/lib/apt/lists/* && \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py button_finder.py ./

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
# Chromium 共享内存调优, 避免 OOM
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

EXPOSE 8924

# 单 worker 即可, 浏览器并发由代码内 asyncio.Semaphore 控制
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8924", "--workers", "1", "--log-level", "info"]
