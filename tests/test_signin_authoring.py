import json
import re

import pytest

from libs.signin_authoring import author_template
from libs.signin_workbench import WorkbenchError


class Model:
    enabled = True

    def __init__(self, selection):
        self.selection = selection
        self.messages = []

    async def chat(self, messages, **options):
        self.messages = messages
        return json.dumps(self.selection)


def entry(url, result, *, method="POST", headers=None):
    return {
        "request": {"url": url, "method": method, "headers": headers or []},
        "response": {"status": 200, "content": {
            "mimeType": "application/json", "text": json.dumps(result, ensure_ascii=False),
        }},
    }


def har(*entries):
    return {"log": {"entries": list(entries)}}


@pytest.mark.asyncio
async def test_authoring_model_sees_only_scoped_evidence():
    model = Model({"steps": [{"id": 1, "condition_indices": [0]}]})
    draft = await author_template(har(
        entry("https://capture.example/api/checkin", {"code": 0, "message": "签到成功", "secret": "hidden-value"},
              headers=[{"name": "Authorization", "value": "Bearer sample-credential-value"}]),
        entry("https://capture.example/login", {"success": True}),
        entry("https://foreign.example/api/checkin", {"success": True}),
    ), "https://capture.example/account", model)

    assert len(draft["steps"]) == 1
    step = draft["steps"][0]
    assert step["request"]["url"] == "https://capture.example/api/checkin"
    assert step["request"]["headers"] == [{"name": "Authorization", "value": "{{authorization}}"}]
    evidence = json.loads(model.messages[-1]["content"])["candidates"]
    assert len(evidence) == 1 and evidence[0]["path"] == "/api/checkin"
    serialized = json.dumps(model.messages)
    assert all(value not in serialized for value in (
        "capture.example", "foreign.example", "sample-credential-value", "hidden-value",
    ))
    pattern = step["rule"]["success_asserts"][0]["re"]
    assert re.search(pattern, '{"code": 0}')
    assert not re.search(pattern, '{"code": 0.4}')


@pytest.mark.asyncio
async def test_missing_success_evidence_draft_fails_closed_in_har_runner():
    model = Model({"steps": [{"id": 1, "condition_indices": []}]})
    draft = await author_template(har(
        entry("https://capture.example/api/checkin", {"message": "working"}),
    ), "https://capture.example", model)
    assert draft["warnings"]
    assert draft["steps"][0]["rule"]["success_asserts"] == [{"re": "(?!)", "from": "content"}]


@pytest.mark.asyncio
async def test_authoring_ignores_protection_page_and_rejects_new_request():
    model = Model({"steps": [{"id": 2, "condition_indices": []}]})
    protected = entry("https://capture.example/api/checkin", {
        "message": "Performing security verification; site verifies you are not a bot",
    })
    with pytest.raises(WorkbenchError, match="安全验证"):
        await author_template(har(protected), "https://capture.example", model)
    assert not model.messages
    with pytest.raises(WorkbenchError, match="不存在"):
        await author_template(har(entry("https://capture.example/api/checkin", {"success": True})),
                              "https://capture.example", model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capture", "change", "expected"),
    [
        ("https://foreign.example/checkin", {}, "没有与填写网址同源"),
        ("https://capture.example/checkin", {"status": 302}, "没有成功返回"),
        ("https://capture.example/checkin?session=sample-private", {}, "参数或格式暂不支持"),
        ("https://capture.example/forum.php", {"content": {}}, "缺少可读取的正文"),
        ("https://capture.example/forum.php", {}, "未识别到签到动作或签到回执"),
    ],
)
async def test_authoring_explains_capture_rejection_before_model(capture, change, expected):
    model = Model({"steps": []})
    request = entry(capture, {"message": "working"}, method="GET")
    request["response"].update(change)
    with pytest.raises(WorkbenchError, match=expected) as failure:
        await author_template(har(request), "https://capture.example", model)
    assert not model.messages
    assert "sample-private" not in str(failure.value)


@pytest.mark.asyncio
async def test_forum_public_route_is_preserved_with_explicit_receipt_only():
    forum_route = "https://capture.example/plugin.php?id=attendance%3Adaily&action=sign"
    model = Model({"steps": [{"id": 1, "condition_indices": [0]}]})
    capture = entry(forum_route, {"message": "签到成功"}, method="GET")
    draft = await author_template(har(capture), "https://capture.example", model)
    assert draft["steps"][0]["request"]["url"] == forum_route
    assert "attendance%3Adaily" not in json.dumps(model.messages)

    model.messages.clear()
    capture["response"]["content"]["text"] = '{"success":true}'
    with pytest.raises(WorkbenchError, match="未识别到签到动作或签到回执"):
        await author_template(har(capture), "https://capture.example", model)
    assert not model.messages


def fixture_step(path, result, *, method="GET", payload=None, mime="application/json"):
    captured = entry("https://capture.example" + path, result, method=method)
    if payload is not None:
        captured["request"]["postData"] = {"mimeType": mime, "text": payload}
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "mime", "location"),
    [
        ("/api/checkin?activity_id=recorded-only", None, None, "query"),
        ("/api/checkin", '{"activity_id":"recorded-only"}', "application/json", "body"),
        ("/api/checkin", "activity_id=recorded-only", "application/x-www-form-urlencoded", "form"),
    ],
)
async def test_business_binding_adds_preparation_and_checks_live_value(path, payload, mime, location):
    model = Model({"steps": [{"id": 2, "condition_indices": [0]}]})
    draft = await author_template(har(
        fixture_step("/api/activity", {"activity_id": "recorded-only", "active": True}),
        fixture_step(path, {"checked_in": True}, method="POST", payload=payload, mime=mime),
    ), "https://capture.example", model)
    assert len(draft["steps"]) == 2
    assert [entry["selected"] for entry in draft["candidates"]] == [True, True]
    binding, = draft["bindings"]
    assert (binding["source_step"], binding["target_step"], binding["location"]) == (1, 2, location)
    variable = binding["variable"]
    assert "recorded-only" not in json.dumps([draft, model.messages])
    assert json.loads(model.messages[-1]["content"])["business_dependencies"] == [
        {"source_id": 1, "target_id": 2, "field": "activity_id"}
    ]

    prep, submission = draft["steps"]
    extraction, = prep["rule"]["extract_variables"]
    assert extraction["name"] == variable
    guard, status = prep["rule"]["success_asserts"]
    live_response = '{"active":true,"activity_id":"new-live-id","info":"example"}'
    assert re.search(extraction["re"], live_response)[1] == "new-live-id"
    assert re.search(guard["re"], live_response)
    assert re.search(status["re"], "204") and not re.search(status["re"], "403")
    for wrong in (
        '{"active":true}', '{"activity_id":123}',
        '{"activity_id":"new-live-id","activity_id":"other"}',
        '{"activity_id":"new-live-id","\\u0061ctivity_id":"other"}',
        '{"data":{"activity_id":"new-live-id"}}',
    ):
        assert not re.search(guard["re"], wrong)
    assert "{{" + variable + "|" in json.dumps(submission["request"])


@pytest.mark.asyncio
async def test_business_binding_leaves_ambiguous_and_non_business_values_unlinked():
    model = Model({"steps": [{"id": 3, "condition_indices": [0]}]})
    draft = await author_template(har(
        fixture_step("/api/activity", {"activity_id": "recorded-only", "reference": "example-reference"}),
        fixture_step("/api/details", {"activity_id": "recorded-only"}),
        fixture_step(
            "/api/checkin?activity_id=recorded-only&reference=example-reference",
            {"checked_in": True}, method="POST",
        ),
    ), "https://capture.example", model)
    assert len(draft["steps"]) == 1
    assert draft["bindings"] == []
    assert "{{query_activity_id|urlencode}}" in draft["steps"][0]["request"]["url"]
    assert "{{query_reference|urlencode}}" in draft["steps"][0]["request"]["url"]
    assert "example-reference" not in json.dumps([draft, model.messages])


@pytest.mark.asyncio
async def test_business_binding_skips_repeated_and_case_colliding_slots():
    model = Model({"steps": [{"id": 2, "condition_indices": [0]}]})
    for query in (
        "activity_id=recorded-only&activity_id=recorded-only",
        "Activity_ID=recorded-only&activity_id=recorded-only",
    ):
        draft = await author_template(har(
            fixture_step("/api/activity", {"activity_id": "recorded-only"}),
            fixture_step("/api/checkin?" + query, {"checked_in": True}, method="POST"),
        ), "https://capture.example", model)
        assert not draft["bindings"]
        assert len(draft["steps"]) == 1
