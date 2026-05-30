# 基础镜像
FROM python:3.10-slim

# 维护者信息
LABEL maintainer="sxl"

# 设置非交互式安装
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /usr/src/app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
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
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
# Playwright 浏览器仅提供 amd64/arm64 预编译包; requirements.txt 已用 platform_machine
# 标记在其余架构(386/armv6/armv7)跳过安装 playwright。故此处据"是否真的装上了
# playwright"决定要不要下载 chromium, 避免在无浏览器的架构上 install 失败导致多架构构建中断。
RUN pip install --no-cache-dir -r requirements.txt && \
    if python -c "import playwright" 2>/dev/null; then \
        playwright install chromium && playwright install-deps chromium; \
    else \
        echo "[build] 当前架构无 playwright 预编译包, 跳过 chromium 安装 (URL 自动抓包功能在该架构不可用)"; \
    fi

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
