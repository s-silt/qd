# 基础镜像
FROM python:3.10-slim

# 维护者信息
LABEL maintainer="sxl"

# 设置工作目录
WORKDIR /usr/src/app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright 依赖
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libgtk-3-0 \
    # 其他依赖
    openssh-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt && \
    # 安装 Playwright 浏览器
    playwright install chromium && \
    playwright install-deps chromium

# 复制项目文件
COPY . .

# 设置权限
RUN chmod +x /usr/src/app/update.sh 2>/dev/null || true

ENV PORT 80
EXPOSE $PORT/tcp

# timezone
ENV TZ=CST-8

# 添加挂载点
VOLUME ["/usr/src/app/config"]

CMD ["sh", "-c", "python /usr/src/app/run.py"]
