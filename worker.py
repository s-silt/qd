# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Author: Binux<i@binux.me>
#         http://binux.me
# Created on 2014-08-09 14:43:13

import asyncio
import datetime
import json
import re
import time
import traceback
from typing import Dict, Optional

import tornado.ioloop
import tornado.log
from tornado import gen
from tornado.concurrent import Future  # used by BatchWorker.done callback

import config
from db import DB
from libs.fetcher import Fetcher
from libs.funcs import Cal, Pusher
from libs.log import Log
from libs.parse_url import parse_url

logger_worker = Log('QD.Worker').getlogger()


class BaseWorker:
    def __init__(self, db: DB):
        self.running = False
        self.db = db
        self.fetcher = Fetcher()

    async def clear_log(self, taskid, sql_session=None):
        """清理单个 task 过期日志，使用批量删除。"""
        log_day = int(
            (await self.db.site.get(
                1,
                fields=('logDay',),
                sql_session=sql_session
            ))['logDay']
        )
        cutoff = time.time() - log_day * 24 * 60 * 60
        logs = await self.db.tasklog.list(
            taskid=taskid,
            fields=('id', 'ctime'),
            sql_session=sql_session,
        )
        expired_ids = [log['id'] for log in logs if log['ctime'] < cutoff]
        if expired_ids:
            await self.db.tasklog.delete(expired_ids, sql_session=sql_session)

    async def push_batch(self):
        """定期推送任务日志。

        优化：原实现存在 N+1 查询——每个 task 单独查 tasklog、每个 tplid 单独查 tpl。
        现在按用户聚合后一次性批量查询，时间复杂度由 O(任务数 × 数据库往返) 降为
        O(用户数 × 数据库往返)。
        """
        try:
            async with self.db.transaction() as sql_session:
                userlist = await self.db.user.list(
                    fields=('id', 'email', 'status', 'push_batch'),
                    sql_session=sql_session
                )
                if not userlist:
                    return
                pushtool = Pusher(self.db, sql_session=sql_session)
                now = time.time()
                for user in userlist:
                    push_batch = json.loads(user['push_batch'])
                    if not (
                        user['status'] == "Enable"
                        and push_batch.get('sw')
                        and isinstance(push_batch.get('time'), (float, int))
                        and now >= push_batch['time']
                    ):
                        continue
                    await self._push_batch_for_user(
                        user, push_batch, pushtool, sql_session
                    )
        except Exception as e:
            logger_worker.error('Push batch task failed: %s', e, exc_info=config.traceback_print)

    async def _push_batch_for_user(self, user, push_batch, pushtool, sql_session):
        userid = user['id']
        logger_worker.debug('User %d check push_batch task, waiting...', userid)
        title = "QD任务日志定期推送"
        delta = push_batch.get("delta", 86400)
        window_start = push_batch['time'] - delta
        window_end = push_batch['time']
        logtemp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(window_end))

        task_list = await self.db.task.list(
            userid=userid,
            fields=(
                'id', 'tplid', 'note', 'disabled',
                'last_success', 'last_failed', 'pushsw'
            ),
            sql_session=sql_session,
        )

        active_tasks = []
        for task in task_list:
            pushsw = json.loads(task['pushsw'])
            if not pushsw.get("pushen"):
                continue
            if (
                task["disabled"] == 0
                or (task.get("last_success", 0) and task['last_success'] >= window_start)
                or (task.get("last_failed", 0) and task['last_failed'] >= window_start)
            ):
                active_tasks.append(task)

        # 一次性批量查询，避免 N+1
        task_ids = [t["id"] for t in active_tasks]
        tpl_ids = list({t["tplid"] for t in active_tasks})
        all_logs = (
            await self.db.tasklog.list(
                taskid=task_ids,
                fields=('taskid', 'success', 'ctime', 'msg'),
                sql_session=sql_session,
                limit=None,
            )
            if task_ids
            else []
        )
        tpl_map: Dict[int, dict] = {}
        if tpl_ids:
            tpl_rows = await self.db.tpl.list(
                id=tpl_ids,
                fields=('id', 'sitename'),
                sql_session=sql_session,
            )
            tpl_map = {row['id']: row for row in tpl_rows}

        logs_by_task: Dict[int, list] = {}
        for log in all_logs:
            if window_start < log['ctime'] <= window_end:
                logs_by_task.setdefault(log['taskid'], []).append(log)

        tmpdict: Dict[int, list] = {}
        numlog = 0
        for task in active_tasks:
            tmp0 = ""
            for log in logs_by_task.get(task["id"], []):
                c_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(log['ctime']))
                tmp0 += f"\\r\\n时间: {c_time}\\r\\n日志: {log['msg']}"
                numlog += 1
            tmplist = tmpdict.get(task['tplid'], [])
            if tmp0:
                tmplist.append(
                    f"\\r\\n-----任务{len(tmplist) + 1}-{task['note']}-----{tmp0}\\r\\n"
                )
            else:
                tmplist.append(
                    f"\\r\\n-----任务{len(tmplist) + 1}-{task['note']}-----\\r\\n记录期间未执行定时任务，请检查任务! \\r\\n"
                )
            tmpdict[task['tplid']] = tmplist

        tmp = ""
        for tmpkey, tmpval in tmpdict.items():
            tpl_row = tpl_map.get(tmpkey)
            if tpl_row:
                tmp = f"\\r\\n\\r\\n=====QD: {tpl_row['sitename']}====="
                tmp += ''.join(tmpval)
                logtemp += tmp

        push_batch["time"] = push_batch['time'] + delta
        if tmp and numlog:
            user_email = user.get('email', 'Unknown')
            logger_worker.debug(
                "Start push batch log for user %s, email:%s", userid, user_email
            )
            await pushtool.pusher(
                userid,
                {"pushen": bool(push_batch.get("sw", False))},
                4080,
                title,
                logtemp,
            )
            logger_worker.info(
                "Complete push batch log for user %s, email:%s", userid, user_email
            )
        else:
            logger_worker.debug(
                'User %s does not need to perform push_batch task, stop.', userid
            )
        await self.db.user.mod(
            userid, push_batch=json.dumps(push_batch), sql_session=sql_session
        )

    # 退避下限: 防止短 interval 模板被一次短暂宕机在几分钟内烧光重试 (#24)
    MIN_BACKOFF = 60

    @staticmethod
    def _is_temporary_error(exc) -> bool:
        """Heuristically decide whether *exc* is a transient (retry-able) error.

        Network/timeout/connection failures and HTTP 5xx / 429 responses are
        treated as temporary so a brief outage cannot permanently disable a
        task (#24).  Anything else (wrong password, captcha, parse error, ...)
        is considered permanent.
        """
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True
        msg = str(exc).lower()
        keywords = (
            'timeout', 'timed out', 'connection', 'reset by peer', 'temporarily',
            'temporary', 'too many requests', 'bad gateway', 'service unavailable',
            'gateway timeout', 'cannot connect', 'connect call failed', 'unreachable',
        )
        if any(k in msg for k in keywords):
            return True
        # HTTP 5xx / 429 status codes
        if re.search(r'\b(429|5\d\d)\b', msg):
            return True
        return False

    @staticmethod
    def failed_count_to_time(
        last_failed_count: int,
        retry_count: int = config.task_max_retry_count,
        retry_interval: Optional[int] = None,
        interval: Optional[int] = None,
        is_temporary: bool = False,
    ) -> Optional[int]:
        """Return seconds until the next retry, or None if no more retries.

        Args:
            last_failed_count: how many times this task has failed consecutively.
            retry_count: maximum allowed retries (-1 = unlimited).
            retry_interval: fixed retry interval in seconds (overrides back-off table).
            interval: tpl-level execution interval used to cap the next wait time.
            is_temporary: when True the failure is transient (network/5xx); the
                retry budget is treated as unlimited so a short outage cannot
                permanently disable the task.  Explicit ``retry_count == 0``
                (user disabled retries) is always honoured.

        Returns:
            Seconds to wait before next attempt, or None if the task should be disabled.
        """
        # Temporary failures never exhaust the budget (unless the user disabled
        # retries entirely with retry_count == 0).
        effective_retry_count = retry_count
        if is_temporary and retry_count != 0:
            effective_retry_count = -1

        next = None
        if last_failed_count < effective_retry_count or effective_retry_count == -1:
            if retry_interval:
                next = retry_interval
            else:
                if last_failed_count == 0:
                    next = 10 * 60
                elif last_failed_count == 1:
                    next = 110 * 60
                elif last_failed_count == 2:
                    next = 4 * 60 * 60
                elif last_failed_count == 3:
                    next = 6 * 60 * 60
                elif last_failed_count < effective_retry_count or effective_retry_count == -1:
                    next = 11 * 60 * 60
                else:
                    next = None
        elif effective_retry_count == 0:
            next = None

        if next and not retry_interval:
            if interval is None:
                interval = 12 * 60 * 60
            next = min(next, interval)
            # 退避下限, 防止短 interval 把退避压平到几秒钟 (#24)
            next = max(next, BaseWorker.MIN_BACKOFF)
        return next

    @staticmethod
    def fix_next_time(next: float, gmt_offset=time.timezone / 60) -> float:
        """
        fix next time to 2:00 - 21:00 (local time), while tpl interval is unset.

        Args:
            next (float): next timestamp
            gmt_offset (float, optional): gmt offset in minutes. Defaults to time.timezone/60.

        Returns:
            next (float): fixed next timestamp
        """
        date = datetime.datetime.fromtimestamp(next, tz=datetime.timezone.utc)
        local_date = date - datetime.timedelta(minutes=gmt_offset)
        if local_date.hour < 2:
            next += 2 * 60 * 60
        if local_date.hour > 21:
            next -= 3 * 60 * 60
        return next

    async def do(self, task):
        is_success = False
        should_push = 0
        userid = None
        title = f"QD 定时任务ID {task['id']}-{task.get('note',None)} 完成"
        content = ""

        # [#23] pushsw 解析失败不得逃出 do(); 用安全默认值兜底, 保证流程继续推进.
        try:
            pushsw = json.loads(task['pushsw'])
        except Exception as e:
            logger_worker.error('taskid:%s parse pushsw failed: %s', task.get('id'), e,
                                exc_info=config.traceback_print)
            pushsw = {}

        tpl = None
        user = None
        new_env = None
        exec_error = None
        start = time.perf_counter()

        # ------------------------------------------------------------------ #
        # Phase 1: 取数 + 校验 + 解密输入 + 执行签到 (do_fetch).
        #   只有"真正执行签到"的步骤(取数/解密/do_fetch)决定成功失败.
        #   [#23] 取数/解密异常也纳入兜底(下方 except), 保证 next 一定推进,
        #         不再 ~500ms 紧抓重试且永不退避.
        # ------------------------------------------------------------------ #
        try:
            async with self.db.transaction() as sql_session:
                user = await self.db.user.get(task['userid'], fields=('id', 'email', 'email_verified', 'nickname', 'logtime'), sql_session=sql_session)
                if not user:
                    await self.db.tasklog.add(task['id'], False, msg='no such user, disabled.', sql_session=sql_session)
                    await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                    return False
                userid = user['id']

                tpl = await self.db.tpl.get(task['tplid'], fields=('id', 'userid', 'sitename', 'siteurl', 'tpl', 'interval', 'last_success'), sql_session=sql_session)
                if not tpl:
                    await self.db.tasklog.add(task['id'], False, msg='tpl missing, task disabled.', sql_session=sql_session)
                    await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                    return False

                if task['disabled']:
                    await self.db.tasklog.add(task['id'], False, msg='task disabled.', sql_session=sql_session)
                    await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                    return False

                if tpl['userid'] and tpl['userid'] != user['id']:
                    await self.db.tasklog.add(task['id'], False, msg='no permission error, task disabled.', sql_session=sql_session)
                    await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                    return False

                fetch_tpl = await self.db.user.decrypt(0 if not tpl['userid'] else task['userid'], tpl['tpl'], sql_session=sql_session)
                env = dict(
                    variables=await self.db.user.decrypt(task['userid'], task['init_env'], sql_session=sql_session),
                    session=[],
                )

                url = parse_url(env['variables'].get('_proxy'))
                if not url:
                    new_env, _ = await self.fetcher.do_fetch(fetch_tpl, env)
                else:
                    proxy = {
                        'scheme': url['scheme'],
                        'host': url['host'],
                        'port': url['port'],
                        'username': url['username'],
                        'password': url['password']
                    }
                    new_env, _ = await self.fetcher.do_fetch(fetch_tpl, env, [proxy])

                is_success = True
        except Exception as e:
            # 取数/解密/do_fetch 任一失败 -> 本次签到失败.
            is_success = False
            exec_error = e
            if config.traceback_print:
                traceback.print_exc()

        # ------------------------------------------------------------------ #
        # Phase 2: 善后(bookkeeping). 独立事务, 失败只记日志,
        #          [B] 绝不回退已成立的成功/失败判定.
        # ------------------------------------------------------------------ #
        if is_success:
            # --- 成功善后: 算下次时间 / 写成功日志 / 更新 task (独立事务) ---
            try:
                async with self.db.transaction() as sql_session:
                    variables = await self.db.user.encrypt(task['userid'], new_env['variables'], sql_session=sql_session)
                    session = await self.db.user.encrypt(task['userid'],
                                                         new_env['session'].to_json() if hasattr(new_env['session'], 'to_json') else new_env['session'], sql_session=sql_session)
                    next = self._cal_success_next(task, tpl)
                    await self.db.tasklog.add(task['id'], success=True, msg=new_env['variables'].get('__log__'), sql_session=sql_session)
                    await self.db.task.mod(task['id'],
                                           last_success=time.time(),
                                           last_failed_count=0,
                                           success_count=task['success_count'] + 1,
                                           env=variables,
                                           session=session,
                                           mtime=time.time(),
                                           next=next,
                                           sql_session=sql_session)
                t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                title = f"QD定时任务 {tpl['sitename']}-{task['note']} 成功"
                content = new_env['variables'].get('__log__')
                content = f"{t} \\r\\n日志：{content}"
                should_push = 0x2
                logger_worker.info('taskid:%d tplid:%d successed! %.5fs',
                                   task['id'], task['tplid'], time.perf_counter() - start)
            except Exception as e:
                # [B] 成功善后写库失败: 仅记日志, 不回退成功判定, 不写失败记录.
                logger_worker.error('taskid:%s tplid:%s post-success bookkeeping failed: %s',
                                    task.get('id'), task.get('tplid'), e, exc_info=config.traceback_print)

            # --- 清理旧日志: 独立事务, 失败只记日志, 不影响成功判定 ([B]) ---
            try:
                async with self.db.transaction() as sql_session:
                    await self.clear_log(task['id'], sql_session=sql_session)
                logger_worker.info(
                    'taskid:%d tplid:%d clear log.', task['id'], task['tplid'])
            except Exception as e:
                logger_worker.error('taskid:%s tplid:%s clear log failed: %s',
                                    task.get('id'), task.get('tplid'), e, exc_info=config.traceback_print)
        else:
            # --- 失败善后: 计算退避 / 写失败日志 / 更新 task (独立事务) ---
            e = exec_error
            try:
                is_temporary = self._is_temporary_error(e)
                next_time_delta = self.failed_count_to_time(
                    task['last_failed_count'], task['retry_count'], task['retry_interval'],
                    tpl['interval'] if tpl else None, is_temporary=is_temporary)

                t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                sitename = tpl['sitename'] if tpl else ''
                title = f"QD定时任务 {sitename}-{task.get('note')} 失败"
                content = f"{t} \\r\\n日志：{e}"
                disabled = False
                if next_time_delta:
                    next = time.time() + next_time_delta
                    content = content + \
                        f" \\r\\n下次运行时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next))}"
                    logtime = json.loads(user['logtime']) if user and user.get('logtime') else {}
                    if 'ErrTolerateCnt' not in logtime:
                        logtime['ErrTolerateCnt'] = 0
                    if logtime['ErrTolerateCnt'] <= task['last_failed_count']:
                        should_push = 0x1
                else:
                    disabled = True
                    next = None
                    content = " \\r\\n任务已禁用"
                    should_push = 0x1

                async with self.db.transaction() as sql_session:
                    await self.db.tasklog.add(task['id'], success=False, msg=str(e), sql_session=sql_session)
                    await self.db.task.mod(task['id'],
                                           last_failed=time.time(),
                                           failed_count=task['failed_count'] + 1,
                                           last_failed_count=task['last_failed_count'] + 1,
                                           disabled=disabled,
                                           mtime=time.time(),
                                           next=next,
                                           sql_session=sql_session)

                logger_worker.error('taskid:%s tplid:%s failed! %.4fs \r\n%s', task.get('id'), task.get('tplid'), time.perf_counter(
                ) - start, str(e).replace('\\r\\n', '\r\n'))
            except Exception as e2:
                # 失败善后本身写库失败: 仅记日志, 不再抛出 (避免 next 不推进).
                logger_worker.error('taskid:%s failure bookkeeping failed: %s',
                                    task.get('id'), e2, exc_info=config.traceback_print)

        # ------------------------------------------------------------------ #
        # [#36] 观测层: 统计 / 推送. 各自独立 try, 在判定提交之后执行,
        #       任何异常都不得影响签到成功/失败判定.
        # ------------------------------------------------------------------ #
        if tpl and tpl.get('id'):
            try:
                async with self.db.transaction() as sql_session:
                    if is_success:
                        await self.db.tpl.incr_success(tpl['id'], sql_session=sql_session)
                    else:
                        await self.db.tpl.incr_failed(tpl['id'], sql_session=sql_session)
            except Exception as e:
                logger_worker.error('taskid:%s update tpl stats failed: %s',
                                    task.get('id'), e, exc_info=config.traceback_print)

        if should_push:
            try:
                # Pass sql_session=None so Pusher opens its own fresh session;
                # the previous session was closed when the transaction block
                # above exited.
                pushtool = Pusher(self.db, sql_session=None)
                await pushtool.pusher(userid, pushsw, should_push, title, content)
            except Exception as e:
                logger_worker.error('taskid:%s push failed! %s', task.get('id'), str(e), exc_info=config.traceback_print)
        return is_success

    def _cal_success_next(self, task, tpl):
        """Compute the next run timestamp for a successful task.

        cal_next_ts returns ``{'r': error}`` without a ``'ts'`` key when the
        ontime/cron expression is invalid; in that case we degrade to the
        interval-based schedule instead of raising KeyError.
        """
        newontime = json.loads(task["newontime"])
        caltool = Cal()
        if newontime.get('sw'):
            if 'mode' not in newontime:
                newontime['mode'] = 'ontime'
            if newontime['mode'] == 'ontime':
                newontime['date'] = (datetime.datetime.now(
                ) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            next = caltool.cal_next_ts(newontime).get('ts')
            if next is None:
                next = self._interval_next(tpl)
        else:
            next = self._interval_next(tpl)
        return next

    @staticmethod
    def _interval_next(tpl):
        """Interval-based next run timestamp (mirrors the legacy success path)."""
        interval = tpl['interval'] if tpl else None
        next = time.time() + max((interval if interval else 24 * 60 * 60), 1 * 60)
        if interval is None:
            next = BaseWorker.fix_next_time(next)
        return next


class QueueWorker(BaseWorker):
    def __init__(self, db: DB):
        logger_worker.info('Queue Worker start...')
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=config.queue_num)
        self.task_lock: Dict = {}
        self.success = 0
        self.failed = 0
        super().__init__(db)

    async def __call__(self):

        asyncio.create_task(self.producer())
        for i in range(config.queue_num):
            asyncio.create_task(self.runner(i))

        while True:
            sleep = asyncio.sleep(config.push_batch_delta)
            if self.success or self.failed:
                logger_worker.info('Last %d seconds, %d task done. %d success, %d failed' ,
                                   config.push_batch_delta, self.success + self.failed, self.success, self.failed)
                self.success = 0
                self.failed = 0
            if config.push_batch_sw:
                await self.push_batch()
            await sleep

    async def runner(self, id):
        logger_worker.debug('Runner %d started' , id)
        while True:
            task = await self.queue.get()
            logger_worker.debug(
                'Runner %d get task: %s, running...' , id, task['id'])
            done = False
            try:
                done = await self.do(task)
            except Exception as e:
                logger_worker.error(
                    'Runner %d get task: %s, failed! %s' , id, task['id'], str(e), exc_info=config.traceback_print)
            if done:
                self.success += 1
                self.task_lock.pop(task['id'], None)
            else:
                self.failed += 1
                self.task_lock[task['id']] = False
            self.queue.task_done()
            # 限速放在循环末尾, 保证每次取任务之间至少间隔 check_task_loop
            # (旧实现把 asyncio.sleep 在阻塞 queue.get 之前创建, 任务耗时超过间隔时
            #  await 立即返回, 起不到限速作用)
            await asyncio.sleep(config.check_task_loop / 1000.0)

    async def producer(self):
        logger_worker.debug('Schedule Producer started')
        while True:
            try:
                tasks = await self.db.task.scan()
                unlock_tasks = 0
                if tasks is not None and len(tasks) > 0:
                    for task in tasks:
                        if not self.task_lock.get(task['id'], False):
                            self.task_lock[task['id']] = True
                            unlock_tasks += 1
                            await self.queue.put(task)
                    if unlock_tasks > 0:
                        logger_worker.debug(
                            'Scaned %d task, put in Queue...', unlock_tasks)
            except Exception as e:
                logger_worker.error(
                    'Schedule Producer get tasks failed! %s', e, exc_info=config.traceback_print)
            await asyncio.sleep(config.check_task_loop / 1000.0)

# 旧版本批量任务定时执行
# 建议仅当新版 Queue 生产者消费者定时执行功能失效时使用


class BatchWorker(BaseWorker):
    def __init__(self, db: DB):
        logger_worker.info('Batch Worker start...')
        super().__init__(db)
        self.running = False

    def __call__(self):
        # self.running = tornado.ioloop.IOLoop.current().spawn_callback(self.run)
        # if self.running:
        #     success, failed = self.running
        #     if success or failed:
        #         logger_worker.info('%d task done. %d success, %d failed' % (success+failed, success, failed))
        if not self.running:
            self.running = gen.convert_yielded(self.run())

        def done(future: Future):
            self.running = False
            success, failed = future.result()
            if success or failed:
                logger_worker.info('%d task done. %d success, %d failed' ,
                                   success + failed, success, failed)
        self.running.add_done_callback(done)

    async def run(self):
        running = []
        success = 0
        failed = 0
        try:
            tasks = await self.db.task.scan()
            if tasks is not None and len(tasks) > 0:
                for task in tasks:
                    running.append(asyncio.ensure_future(self.do(task)))
                    if len(running) >= 50:
                        logger_worker.debug(
                            'scaned %d task, waiting...', len(running))
                        result = await asyncio.gather(*running[:10])
                        for each in result:
                            if each:
                                success += 1
                            else:
                                failed += 1
                        running = running[10:]
                logger_worker.debug('scaned %d task, waiting...', len(running))
                result = await asyncio.gather(*running)
                for each in result:
                    if each:
                        success += 1
                    else:
                        failed += 1
            if config.push_batch_sw:
                await self.push_batch()
        except Exception as e:
            logger_worker.exception(e)
        return (success, failed)


if __name__ == '__main__':
    tornado.log.enable_pretty_logging()
    io_loop = tornado.ioloop.IOLoop.instance()
    if config.worker_method.upper() == 'QUEUE':
        queue_worker = QueueWorker(DB())
        io_loop.add_callback(queue_worker)
    elif config.worker_method.upper() == 'BATCH':
        batch_worker = BatchWorker(DB())
        tornado.ioloop.PeriodicCallback(batch_worker, config.check_task_loop).start()
        # worker()
    else:
        raise RuntimeError('Worker_method must be Queue or Batch')

    io_loop.start()
