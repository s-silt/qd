"""Exercise the PCBeta task workflow in Chromium with intercepted, local HTML."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from libs import signin_pcbeta, signin_workbench
from libs.signin_recording import record_workflow
from libs.signin_workbench import VerificationNeeded


DAILY_ID = "47"
OTHER_ID = "81"
DAILY_TITLE = "每日打卡"
DETAIL = f"/home.php?mod=task&do=view&id={DAILY_ID}"
APPLY = f"/home.php?mod=task&do=apply&id={DAILY_ID}"
COMPLETED = "/home.php?mod=task&item=done"
SHANGHAI = timezone(timedelta(hours=8))


def page_html(main, active="new"):
    tabs = [("new", "新任务"), ("doing", "进行中的任务"), ("done", "已完成的任务")]
    navigation = "".join(
        f'<li class="{"a" if key == active else ""}"><a href="home.php?mod=task&amp;item={key}">{label}</a></li>'
        for key, label in tabs
    )
    return (
        '<!doctype html><meta charset="utf-8"><title>站点任务</title>'
        '<div id="pt"><a href="home.php?mod=task">任务</a></div>'
        f'<div id="ct" class="ct2_a wp cl"><div class="mn"><div class="bm bw0">{main}</div></div>'
        f'<div class="appl"><div class="tbn"><h2>任务</h2><ul>{navigation}</ul></div></div></div>'
    )


def task_row(task_id, title, reward, status):
    return (
        '<tr><td class="bbda" width="80"></td><td class="bbda ptm pbm">'
        f'<h3 class="xs2 xi2"><a href="home.php?mod=task&amp;do=view&amp;id={task_id}">{title}</a></h3>'
        '<p class="xg2">站点任务说明</p></td>'
        f'<td class="xi1 bbda hm" width="200">积分 PB币 {reward}</td>'
        f'<td class="bbda" width="120"><p>{status}</p></td></tr>'
    )


class TaskBoard:
    def __init__(self, state):
        today = datetime.now(SHANGHAI).date()
        self.today = f"{today.isoformat()} 08:17"
        self.other_time = f"{today.isoformat()} 06:45"
        self.yesterday = f"{(today - timedelta(days=1)).isoformat()} 09:23"
        self.state = state
        self.paths = []
        self.actions = []
        self.disallowed = []

    @property
    def done(self):
        return self.state == "already" or (self.state == "fresh" and bool(self.actions))

    def task_list(self):
        # A similar title and a direct list action must not be mistaken for the exact task detail.
        other = task_row(OTHER_ID, "每日打卡活动", 999, "可申请")
        action = f'<a class="taskbtn" href="{escape(APPLY)}">立即申请</a>'
        daily = "" if self.done else task_row(DAILY_ID, DAILY_TITLE, 7, action)
        return page_html(f'<h1 class="mt">新任务</h1><div class="ptm"><table>{other}{daily}</table></div>')

    def detail(self):
        action = f'<a class="taskbtn" href="{escape(APPLY)}">立即申请</a>'
        return page_html(
            '<h1 class="mt cl">任务详情</h1><div><table class="tfm"><tr>'
            f'<td class="bbda"><div class="xs2 ptm pbm">{DAILY_TITLE}</div></td></tr></table>'
            '<table><tr><th class="bbda">任务奖励</th><td class="bbda">积分 PB币 7</td></tr></table>'
            f'<div class="mtw">{action}</div></div>'
        )

    def completed(self):
        other_title = DAILY_TITLE if self.state == "different-task" else "每日打卡活动"
        other = task_row(OTHER_ID, other_title, 999, f"完成于 {self.other_time}")
        daily = ""
        if self.done or self.state == "stale":
            when = self.today if self.done else self.yesterday
            if self.state == "fresh":
                when = f'<span title="{when}">刚刚</span>'
            daily = task_row(DAILY_ID, DAILY_TITLE, 7, f"完成于 {when}")
        return page_html(
            f'<div class="ptm"><table>{other}{daily}</table></div>',
            active="done",
        )

    async def handle(self, route):
        req = route.request
        url = urlsplit(req.url)
        if f"{url.scheme}://{url.netloc}" != signin_pcbeta.ORIGIN or url.path != "/home.php" or req.method != "GET":
            self.disallowed.append(url.path)
            await route.abort()
            return
        self.paths.append(f"{url.path}?{url.query}")
        query = parse_qs(url.query)
        if query == {"mod": ["task"], "item": ["done"]}:
            body = self.completed()
        elif query in ({"mod": ["task"]}, {"mod": ["task"], "item": ["new"]}):
            body = self.task_list()
        elif query == {"mod": ["task"], "do": ["view"], "id": [DAILY_ID]}:
            body = self.detail()
        elif query == {"mod": ["task"], "do": ["apply"], "id": [DAILY_ID]}:
            self.actions.append(req.headers.get("referer"))
            # Discuz may pass through a notice after applying. The completed row is authoritative.
            body = page_html('<div id="messagetext">任务申请已提交。</div>')
        else:
            self.disallowed.append(url.path)
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


@pytest.fixture
async def task_chromium():
    playwright = pytest.importorskip("playwright.async_api")
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_PATH")
    windows_chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not executable and windows_chrome.is_file():
        executable = str(windows_chrome)
    launch = {"executable_path": executable} if executable else {}
    async with playwright.async_playwright() as player:
        try:
            browser = await player.chromium.launch(headless=True, **launch)
        except playwright.Error:
            pytest.skip("Chromium runtime unavailable")
        try:
            yield browser
        finally:
            await browser.close()


async def open_board(browser, state):
    board = TaskBoard(state)
    context = await browser.new_context(service_workers="block")
    await context.route("**/*", board.handle)
    page = await context.new_page()
    await page.goto(signin_pcbeta.START_URL)
    return board, page


async def page_is_authorized():
    return None


async def test_daily_clicks_exact_detail_once_and_reads_its_own_receipt(task_chromium):
    board, page = await open_board(task_chromium, "fresh")
    result = await signin_pcbeta.run_daily_checkin(page, page_is_authorized)
    assert board.disallowed == []
    assert board.actions == [signin_pcbeta.ORIGIN + DETAIL]
    assert board.paths.index(DETAIL) < board.paths.index(APPLY)
    assert COMPLETED in board.paths[board.paths.index(APPLY) + 1:]
    assert "每日打卡" in result and ("完成" in result or "成功" in result)
    assert board.today in result
    reward_text = "".join(result.split())
    assert any(clue in reward_text for clue in ("7PB", "PB币7", "PB7"))
    assert board.other_time not in result and "999" not in result


async def test_completed_daily_task_does_not_click_from_any_page(task_chromium):
    board, page = await open_board(task_chromium, "already")
    result = await signin_pcbeta.run_daily_checkin(page, page_is_authorized)
    assert "已完成" in result and board.today in result and "PB" in result
    assert not board.actions and not board.disallowed


@pytest.mark.parametrize("state", ["missing", "stale"])
async def test_missing_or_stale_receipt_never_repeats_application(task_chromium, state):
    board, page = await open_board(task_chromium, state)
    with pytest.raises(signin_pcbeta.DailyTaskError):
        await signin_pcbeta.run_daily_checkin(page, page_is_authorized)
    assert board.actions == [signin_pcbeta.ORIGIN + DETAIL]
    assert not board.disallowed


async def test_completed_row_with_different_task_id_cannot_confirm_application(task_chromium):
    board, page = await open_board(task_chromium, "different-task")
    with pytest.raises(signin_pcbeta.DailyTaskError):
        await signin_pcbeta.run_daily_checkin(page, page_is_authorized)
    assert board.actions in ([], [signin_pcbeta.ORIGIN + DETAIL])
    assert not board.disallowed


async def test_auth_callback_stops_before_application(task_chromium):
    board, page = await open_board(task_chromium, "fresh")

    async def check_page():
        if page.url == signin_pcbeta.ORIGIN + DETAIL:
            raise VerificationNeeded("站点要求人工验证")

    with pytest.raises(VerificationNeeded, match="人工验证"):
        await signin_pcbeta.run_daily_checkin(page, check_page)
    assert DETAIL in board.paths
    assert not board.actions and not board.disallowed


async def test_online_workbench_creates_valid_recipe_with_model_disabled(task_chromium):
    board, page = await open_board(task_chromium, "fresh")
    session = signin_workbench.BrowserSession(1, signin_pcbeta.START_URL)
    session.page = page
    session.context = page.context

    draft = (await record_workflow(session, "auto", SimpleNamespace(enabled=False)))["draft"]
    assert board.actions == [signin_pcbeta.ORIGIN + DETAIL]
    assert not board.disallowed
    assert draft["verified"] and draft["full_steps"]
    assert board.today in draft["summary"] and "PB" in draft["summary"]
    script = json.loads(json.dumps(draft["script"]))
    assert script["recipe"] == signin_pcbeta.RECIPE
    assert [step["text"] for step in script["steps"]] == list(signin_pcbeta.STEP_TEXTS)
    assert signin_workbench.validate_script(script) == script


async def test_saved_recipe_replay_preserves_already_done_receipt(task_chromium, monkeypatch):
    board = TaskBoard("already")
    state = {"cookies": [], "origins": []}

    @asynccontextmanager
    async def isolated_fixture(*, storage_state):
        assert storage_state == state
        context = await task_chromium.new_context(service_workers="block", storage_state=storage_state)
        create_page = context.new_page

        async def new_page():
            page = await create_page()
            await page.route("**/*", board.handle)
            return page

        monkeypatch.setattr(context, "new_page", new_page)
        try:
            yield context
        finally:
            await context.close()

    async def validate_local_fixture(url):
        assert url == signin_pcbeta.START_URL

    monkeypatch.setattr(signin_workbench, "isolated_browser", isolated_fixture)
    monkeypatch.setattr(signin_workbench, "validate_start_url", validate_local_fixture)
    stored_script = json.loads(json.dumps(signin_workbench.pcbeta_daily_script()))
    result, returned_state = await signin_workbench.replay(stored_script, state)
    assert "已完成" in result and board.today in result and "PB" in result
    assert returned_state == state
    assert not board.actions and not board.disallowed
