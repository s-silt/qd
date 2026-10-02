# -*- coding: utf-8 -*-
"""调度纯函数与源码保持一致，不再在测试里复制一份退避表。"""
import asyncio
import datetime

import pytest

from worker import BaseWorker, QueueWorker, TaskRunResult


def test_backoff_matches_public_helper():
    assert BaseWorker.failed_count_to_time(0, retry_count=8) == 10 * 60
    assert BaseWorker.failed_count_to_time(8, retry_count=8) is None
    assert BaseWorker.failed_count_to_time(0, retry_count=8, interval=120) == 120


def test_fix_next_time_night_window():
    # 00:30 UTC, offset 0 → 加 2 小时
    ts = datetime.datetime(2026, 1, 1, 0, 30, tzinfo=datetime.timezone.utc).timestamp()
    fixed = BaseWorker.fix_next_time(ts, gmt_offset=0)
    assert fixed == ts + 2 * 60 * 60


def test_temporary_error_classification():
    assert BaseWorker._is_temporary_error(TimeoutError("timed out")) is True
    assert BaseWorker._is_temporary_error(RuntimeError("密码错误, 请稍后再试")) is False
    assert BaseWorker._is_temporary_error(RuntimeError("http status: 503")) is True


def test_task_run_result_records_reason_without_replacing_bool_api():
    ok = TaskRunResult(True, 1)
    bad = TaskRunResult(False, 1, "boom")
    assert ok.success is True
    assert bad.success is False
    assert bad.reason == "boom"
    assert bool(ok) is True
    assert bool(bad) is False


@pytest.mark.asyncio
async def test_queue_runner_exception_updates_last_result(monkeypatch):
    """NOTES#4: do() 抛出时 runner 必须刷新 last_result，不能残留上一任务终态。"""
    qw = QueueWorker(db=None)  # type: ignore[arg-type]
    qw.last_result = TaskRunResult(True, 99, "stale-success")
    qw.queue = asyncio.Queue()
    qw.task_lock = {7: True}

    async def boom(_task):
        raise RuntimeError("runner-boom")

    monkeypatch.setattr(qw, "do", boom)
    monkeypatch.setattr("worker.config.check_task_loop", 1)  # 1ms sleep

    await qw.queue.put({"id": 7})
    # 跑一轮 runner：取任务 → 异常 → 写 last_result → task_done → sleep
    task = asyncio.create_task(qw.runner(0))
    await asyncio.wait_for(qw.queue.join(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert qw.last_result is not None
    assert qw.last_result.success is False
    assert qw.last_result.task_id == 7
    assert "runner-boom" in qw.last_result.reason
    assert qw.failed == 1
    assert qw.success == 0
    assert qw.task_lock.get(7) is False
