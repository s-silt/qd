"""Exercise deterministic auto recording without a site or model service."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from libs import signin_workbench as workbench
from libs.signin_recording import record_workflow


ORIGIN = "https://recording.example"
SITE = ORIGIN + "/checkin"


class Model:
    enabled = True

    async def chat(self, messages, **options):
        observed = json.loads(messages[-1]["content"])["actions"]
        # The model attempts to delete all intermediates and supply its own selector.
        return json.dumps({"summary": "签到", "steps": [
            {"id": observed[0]["id"], "label": "第一步", "selector": "#ignored"},
            {"id": observed[-1]["id"], "label": "最后一步"},
        ]})


class Page:
    def __init__(self, stages):
        self.url = SITE
        self.stages = stages
        self.index = 0
        self.clicks = []
        self.challenge = False
        self.signal = ""
        self.session = None
        self.on_click = None

    def is_closed(self):
        return False

    async def evaluate(self, source):
        assert source == workbench.JS_FIND_CANDIDATES
        return [
            {"text": text, "selector": f"#step-{self.index}-{index}"}
            for index, text in enumerate(self.stages[self.index])
        ]

    async def wait_for_function(self, source, *, arg, timeout):
        assert source == workbench._AUTO_PROGRESS

    def locator(self, selector):
        index = int(selector.split("-")[-1])
        text = self.stages[self.index][index]
        return Locator(self, text)


class Locator:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    async def count(self):
        return 1

    async def is_enabled(self):
        return True

    async def evaluate(self, source, *, timeout):
        if source == workbench._FIND_TARGET:
            return {"text": self.text, "tag": "button", "href": ""}
        assert source == workbench._AUTO_TARGET
        return {"link": None, "form": None, "baseTarget": "", "disabled": False, "download": False}

    async def click(self, *, trial=False, timeout):
        if trial:
            return
        page = self.page
        page.clicks.append(self.text)
        page.session.recording.summaries.append({"after_action": len(page.clicks)})
        page.index = min(page.index + 1, len(page.stages) - 1)
        if page.on_click:
            await page.on_click(self.text)


@pytest.fixture
def recording(monkeypatch):
    async def challenge(page):
        return page.challenge

    async def success(page):
        return page.signal

    monkeypatch.setattr(workbench, "challenge_visible", challenge)
    monkeypatch.setattr(workbench, "success_signal", success)
    monkeypatch.setattr(workbench, "settle", AsyncMock())

    def setup(*stages):
        session = workbench.BrowserSession(1, SITE)
        page = Page(stages)
        page.session = session
        session.page = page
        session.context = SimpleNamespace(route=AsyncMock(), unroute=AsyncMock())
        return session, page

    return setup


@pytest.mark.parametrize("path", [
    "/plugin.php?id=attendance:daily",
    "/forum/plugin.php?action=sign&id=sample_qd%3Aaction&page=index",
    "/forum/plugin.php?id=sample:checkin",
    "/home.php?mod=task",
    "/forum/home.php?mod=task",
    "/account/checkin",
])
def test_browser_recording_accepts_public_sign_in_pages(path):
    assert workbench.can_record(ORIGIN, ORIGIN + path)


@pytest.mark.parametrize("path", [
    "/plugin.php?id=attendance:daily&id=attendance:daily",
    "/plugin.php?id=attendance:daily&ID=sample:sign",
    "/plugin.php?id=attendance:daily&formhash=fixture",
    "/plugin.php?id=attendance:daily&next=checkin",
    "/plugin.php?id=attendance:daily&mod=password",
    "/plugin.php?id=attendance:daily&action=login",
    "/plugin.php?id=attendance:daily&page=456789",
    "/plugin.php?id=attendance:daily&action=%2573ign",
    "/plugin.php?id=attendance%253Adaily",
    "/plugin.php?id=attendance%3Adaily%26action%3Dlogin",
    "/plugin.php?id=attendance:daily;action=login",
    "/plugin.php?id=sample:security",
    "/plugin.php?id=session_qd:daily",
    "/plugin.php?id=fictionaltoken_qd:daily",
    "/plugin.php?id=fixture984651_qd:daily",
    "/plugin.php?id=mail_qd:authorize",
    "/plugin.php?id=unrelated:daily",
    "/plugin.php?id=attendance:daily&",
    "/account/plugin.php?id=attendance:daily&session=fixture",
    "/%6Cogin/plugin.php?id=attendance:daily",
    "/other?id=attendance:daily",
    "/home.php?mod=task&do=apply",
    "/home.php?mod=task&do=draw",
    "/home.php?mod=task&do=view",
    "/home.php?mod=task&mod=task",
    "/home.php?mod=task&session=fixture",
    "/login/home.php?mod=task",
])
def test_browser_recording_rejects_private_or_ambiguous_query(path):
    assert not workbench.can_record(ORIGIN, ORIGIN + path)


@pytest.mark.asyncio
async def test_browser_recording_preserves_plugin_query_in_validated_script(recording):
    session, page = recording(["签到"], [])
    route = ORIGIN + "/forum/plugin.php?action=sign&id=sample_qd%3aaction"
    page.url = route + "#calendar"

    async def site_outcome(_):
        page.signal = "签到成功"

    page.on_click = site_outcome
    draft = (await record_workflow(session, "auto", Model()))["draft"]
    assert draft["script"]["url"] == route
    assert workbench.validate_script(draft["script"]) == draft["script"]
    assert not workbench.can_record("https://other.example", route)


@pytest.mark.asyncio
async def test_public_task_landing_never_selects_generic_task_controls(recording):
    session, page = recording(["申请任务", "领取奖励"])
    page.url = ORIGIN + "/home.php?mod=task"
    result = await record_workflow(session, "auto", Model())
    assert result["manual"] and result["recorded_steps"] == 0
    assert not page.clicks


@pytest.mark.asyncio
async def test_auto_keeps_all_observed_steps_and_only_claims_signin_rewards(recording):
    session, page = recording(
        ["开始签到任务", "开始任务"], ["继续签到", "领取奖励"], ["签到"],
        ["领取签到奖励"], ["领取奖励"],
    )

    async def site_outcome(text):
        if text == "签到":
            page.signal = "签到成功"

    page.on_click = site_outcome
    assert not page.clicks
    draft = (await record_workflow(session, "auto", Model()))["draft"]
    assert page.clicks == ["开始签到任务", "继续签到", "签到", "领取签到奖励"]
    assert draft["verified"] and draft["full_steps"] and draft["steps"] == 4
    assert [step["text"] for step in draft["script"]["steps"]] == page.clicks
    assert draft["script"]["labels"][1] == "执行录制的操作 2"
    assert draft["script"]["steps"][0]["selector"] == "#step-0-0"
    session.context.unroute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_stage, reason", [
    (["继续签到", "签到"], "多个"),
    (["开始签到任务"], "已执行过"),
    (["继续任务", "领取奖励"], "没有可明确识别"),
])
async def test_auto_hands_off_without_repeating_the_clicked_step(recording, next_stage, reason):
    session, page = recording(["开始签到任务"], next_stage)
    result = await record_workflow(session, "auto", Model())
    assert result["manual"] and result["recorded_steps"] == 1 and reason in result["reason"]
    assert page.clicks == ["开始签到任务"]
    assert [step["text"] for step in session.recording.steps] == page.clicks
    session.context.unroute.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_stops_when_the_site_requires_verification(recording):
    session, page = recording(["开始签到任务"], ["继续签到"])

    async def site_outcome(_):
        page.challenge = True

    page.on_click = site_outcome
    with pytest.raises(workbench.VerificationNeeded):
        await record_workflow(session, "auto", Model())
    assert page.clicks == ["开始签到任务"]
    assert session.recording is None and session.captured is None
    session.context.unroute.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_aborts_cross_site_navigation_before_following_it(recording):
    session, page = recording(["开始签到任务"], ["继续签到"])
    route = SimpleNamespace(
        request=SimpleNamespace(url="https://other.example/page", method="GET", is_navigation_request=lambda: True),
        abort=AsyncMock(), fallback=AsyncMock(),
    )

    async def site_outcome(_):
        guard = session.context.route.await_args.args[1]
        await guard(route)

    page.on_click = site_outcome
    with pytest.raises(workbench.WorkbenchError, match="其他站点"):
        await record_workflow(session, "auto", Model())
    assert page.clicks == ["开始签到任务"]
    route.abort.assert_awaited_once()
    route.fallback.assert_not_awaited()
    assert session.recording is None and session.captured is None
