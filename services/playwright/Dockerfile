# Playwright sidecar 镜像
# 提供「URL → 自动抓 HAR」能力, 由 QD 主容器通过 HTTP 调用。
# 用法见 docker-compose.local.yml 中的 playwright 服务定义。

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
