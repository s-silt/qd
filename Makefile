.PHONY: help install dev test lint run clean

help:  ## 显示帮助信息
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## 安装依赖
	pip install -r requirements.txt

dev:  ## 安装开发依赖
	pip install -r requirements.txt
	pip install pytest flake8 black isort mypy

test:  ## 运行测试
	python -m pytest tests/ -v

lint:  ## 代码检查
	flake8 --max-line-length 120 --ignore E501,W503 .
	black --check --line-length 120 .
	isort --check --profile black .

lint-encoding:  ## 检查编码异常字符 (U+FFFD, U+200B 等)
	@bash scripts/check-encoding.sh

format:  ## 代码格式化
	black --line-length 120 .
	isort --profile black .

run:  ## 启动服务
	python run.py

clean:  ## 清理缓存
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache

docker-build:  ## 构建 Docker 镜像
	docker build -t qd:local .

docker-up:  ## 启动 Docker Compose
	docker-compose up -d

docker-down:  ## 停止 Docker Compose
	docker-compose down
