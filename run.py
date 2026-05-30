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


async def _init_database(logger):
    """初始化数据库并执行 schema 迁移, 对"数据库尚未就绪"做有限退避重试。

    Docker / Compose 部署中, 数据库容器(尤其 MySQL 首次初始化)常晚于 QD 就绪,
    depends_on 也只保证"已启动"而非"可连接"。若此处不重试, 首次连接失败会直接
    抛出 OperationalError, 导致容器一启动就退出(表现为"构建成功但起不来")。
    重试次数与间隔可通过 DB_CONNECT_MAX_RETRY / DB_CONNECT_RETRY_INTERVAL 调整。
    """
    from sqlalchemy.exc import InterfaceError, OperationalError

    database = DB()
    converter = db_converter.DBconverter(database)
    max_retry = config.db_connect_max_retry
    interval = config.db_connect_retry_interval
    attempt = 0
    while True:
        try:
            await converter.convert_new_type(database)
            return database
        # 仅对"连接类"错误重试; schema/SQL 等逻辑错误立即抛出, 不被掩盖
        except (OperationalError, InterfaceError, OSError) as e:
            attempt += 1
            # max_retry < 0 表示无限重试, 一直等到数据库就绪
            if 0 <= max_retry < attempt:
                logger.error(
                    "数据库连接失败, 已重试 %s 次仍无法连接, 放弃启动: %s", max_retry, e
                )
                raise
            wait = min(interval * attempt, 30.0)  # 线性退避, 单次上限 30s
            logger.warning(
                "数据库尚未就绪 (第 %s 次尝试), %.1fs 后重试: %s", attempt, wait, e
            )
            await engine.dispose()  # 释放本次失败的连接, 下次重试用全新连接
            await asyncio.sleep(wait)


def start_server():
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
        database = asyncio.run(_init_database(logger_qd))

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
    except KeyboardInterrupt :
        logger_qd.info("Http Server is being manually interrupted... ")
        asyncio.run(engine.dispose())
        logger_qd.info("Http Server is ended. ")


if __name__ == "__main__":
    start_server()
