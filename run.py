# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2016 Binux <roy@binux.me>

import asyncio
import json
import logging
import os
import platform
import sys

import tornado.log
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop, PeriodicCallback

import config
from db import DB, db_converter
from db.basedb import engine
from libs.log import Log
from web.app import Application
from worker import BatchWorker, QueueWorker

if sys.getdefaultencoding() != 'utf-8':
    import importlib
    importlib.reload(sys)


def _check_default_secrets(logger):
    """启动时检查关键密钥是否仍为默认值，是则醒目告警。

    生产部署（特别是 Docker 镜像）极易忘记覆盖默认 Cookie/AES 密钥，
    本检查不会阻止启动，但会输出 WARNING 提示用户。
    """
    import hashlib
    default_secret = hashlib.sha256(b'binux').digest()
    if config.cookie_secret == default_secret:
        logger.warning(
            "[安全] COOKIE_SECRET 未设置, 当前为默认值 'binux'。"
            "强烈建议通过环境变量覆盖, 例如: -e COOKIE_SECRET=$(openssl rand -hex 32)"
        )
    if config.aes_key == default_secret:
        logger.warning(
            "[安全] AES_KEY 未设置, 当前为默认值 'binux'。"
            "已存储的加密数据可被任何人解密, 建议生产环境覆盖该变量。"
        )
    if not config.domain:
        logger.warning(
            "[配置] DOMAIN 未设置, 邮件链接、推送链接将无法生成正确域名。"
        )
    # 邮件密码 / Mailgun key 若已配置却留空则功能失效，但不属于安全问题；
    # 若配置了 SMTP 服务器但没有配置密码，才需要告警（可能误用明文 relay）。
    if config.mail_smtp and not config.mail_password and not config.mailgun_key:
        logger.warning(
            "[安全] MAIL_SMTP 已配置但 MAIL_PASSWORD 为空，邮件将以无认证方式发送。"
            "如果 SMTP 服务器需要认证，请设置 MAIL_PASSWORD 环境变量。"
        )
    if config.mailgun_key and config.mailgun_key == "":
        # 空字符串不会走到这里，这个分支永远不触发，留作占位
        pass  # pragma: no cover


async def _start_worker_async(db):
    """在 asyncio 事件循环中启动 worker（供 FastAPI lifespan 调用）。

    QueueWorker 是纯 asyncio 协程，直接用 asyncio.create_task 启动。
    BatchWorker 原本依赖 tornado.ioloop.PeriodicCallback；在 FastAPI 模式下
    用等效的 asyncio sleep 循环替代，保持调度逻辑不变。
    """
    logger = Log('QD.Run').getlogger()
    try:
        if config.worker_method.upper() == 'QUEUE':
            worker = QueueWorker(db)
            asyncio.create_task(worker())
            logger.info("QueueWorker started as asyncio task")
        elif config.worker_method.upper() == 'BATCH':
            worker = BatchWorker(db)

            async def _batch_loop():
                interval_s = config.check_task_loop / 1000.0
                while True:
                    worker()
                    await asyncio.sleep(interval_s)

            asyncio.create_task(_batch_loop())
            logger.info("BatchWorker started as asyncio periodic task (interval=%sms)",
                        config.check_task_loop)
        else:
            raise RuntimeError('worker_method must be Queue or Batch, please check config!')
    except Exception as e:
        logger.exception('worker start error: %s', e)
        raise


def start_server_fastapi():
    """Start the FastAPI/uvicorn server with integrated worker."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("uvicorn is not installed. Run: pip install 'uvicorn[standard]'", file=sys.stderr)
        sys.exit(1)

    try:
        from contextlib import asynccontextmanager
        from db import DB
        from libs.fetcher import Fetcher
        from web.fastapi_app import create_app
    except ImportError as e:  # pragma: no cover
        print(f"Import error during startup: {e}", file=sys.stderr)
        sys.exit(1)

    logger = Log('QD.Run').getlogger()

    # Check default secrets (mirrors Tornado behaviour)
    _check_default_secrets(logger)

    # Determine port: honour -p <port> CLI arg same as Tornado launcher
    if len(sys.argv) > 2 and sys.argv[1] == '-p' and sys.argv[2].isdigit():
        port = int(sys.argv[2])
    else:
        port = int(os.getenv("FASTAPI_PORT", str(config.port)))

    # Read version
    version = "Debug"
    try:
        _version_path = os.path.join(os.path.dirname(__file__), "version.json")
        with open(_version_path, "r", encoding="utf-8") as f:
            version = str(json.load(f).get("version", "Debug"))
    except Exception as e:
        logger.warning("Could not read version.json: %s", e)

    logger.info("Initialising DB …")
    db = DB()

    # Run DB conversion (same as Tornado path)
    asyncio.run(db_converter.DBconverter(db).convert_new_type(db))

    logger.info("Initialising Fetcher …")
    fetcher = Fetcher()

    @asynccontextmanager
    async def lifespan(app):  # noqa: ANN001
        """FastAPI lifespan: start worker on startup, dispose DB on shutdown."""
        await _start_worker_async(db)
        logger.info("FastAPI server ready on %s:%d", config.bind, port)
        yield
        # Shutdown: dispose SQLAlchemy engine
        await engine.dispose()
        logger.info("FastAPI server shutdown complete.")

    logger.info("Building FastAPI app (version=%s) …", version)
    app = create_app(db, fetcher, version=version, lifespan=lifespan)

    logger.info("FastAPI server starting on %s:%d", config.bind, port)

    uvicorn.run(
        app,
        host=config.bind,
        port=port,
        log_level="debug" if config.debug else "info",
        access_log=False,
    )


def start_server_tornado():
    """Start the original Tornado HTTP server with worker (legacy launcher)."""
    # init logging
    logger = Log().getlogger()
    logger_qd = Log('QD.Run').getlogger()

    if config.debug:
        channel = logging.StreamHandler(sys.stderr)
        channel.setFormatter(tornado.log.LogFormatter())
        channel.setLevel(logging.WARNING)
        logger_qd.addHandler(channel)

    if not config.accesslog:
        tornado.log.access_log.disabled = True
    else:
        tornado.log.access_log = Log('tornado.access').getlogger()
        # tornado.log.app_log = Log('tornado.application').getlogger()

    if len(sys.argv) > 2 and sys.argv[1] == '-p' and sys.argv[2].isdigit():
        port = int(sys.argv[2])
    else:
        port = config.port

    if platform.system() == 'Windows':
        config.multiprocess = False
    if config.multiprocess and config.autoreload:
        config.autoreload = False

    _check_default_secrets(logger_qd)

    try:
        database = DB()
        converter = db_converter.DBconverter(database)
        asyncio.run(converter.convert_new_type(database))

        with open(os.path.join(os.path.dirname(__file__), 'version.json'), 'r', encoding='utf-8') as _f:
            default_version = json.load(_f)['version']
        app = Application(database, default_version)
        http_server = HTTPServer(app, xheaders=True)
        http_server.bind(port, config.bind)
        if config.multiprocess:
            http_server.start(num_processes=0)
        else:
            http_server.start()

        io_loop = IOLoop.instance()
        try:
            if config.worker_method.upper() == 'QUEUE':
                worker = QueueWorker(database)
                io_loop.add_callback(worker)
            elif config.worker_method.upper() == 'BATCH':
                worker = BatchWorker(database)
                PeriodicCallback(worker, config.check_task_loop).start()
            else:
                raise RuntimeError('worker_method must be Queue or Batch, please check config!')
        except Exception as e:
            logger.exception('worker start error: %s', e)
            raise KeyboardInterrupt() from e

        logger_qd.info("Http Server started on %s:%s", config.bind, port)
        io_loop.start()
    except KeyboardInterrupt:
        logger_qd.info("Http Server is being manually interrupted... ")
        asyncio.run(engine.dispose())
        logger_qd.info("Http Server is ended. ")


def main():
    """Entry point: dispatch to FastAPI or Tornado based on WEB_FRAMEWORK env var."""
    fw = os.getenv("WEB_FRAMEWORK", "fastapi").lower()
    if fw == "fastapi":
        start_server_fastapi()
    elif fw == "tornado":
        start_server_tornado()
    else:
        raise ValueError(f"Unsupported WEB_FRAMEWORK: {fw!r}. Choose 'fastapi' or 'tornado'.")


if __name__ == "__main__":
    main()
