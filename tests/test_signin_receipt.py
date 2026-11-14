import json
from types import SimpleNamespace

import pytest

from libs.signin_receipt import SigninTrial


def observe(trial, body, *, code=200, path="/api/checkin", mime="application/json",
            success=True, assertions=None, step=1):
    url = f"https://example.test{path}"
    entry = {"idx": step, "request": {"url": url}}
    response = SimpleNamespace(
        effective_url=url, code=code, headers={"Content-Type": mime},
        body=json.dumps(body, ensure_ascii=False).encode() if mime == "application/json" else body.encode(),
    )
    trial.observe(entry, response, success, assertions or {})


def test_trial_separates_rules_from_allowlisted_site_receipt():
    trial = SigninTrial()
    observe(trial, {"status": 0}, path="/api/activity", assertions={"content": True})
    observe(trial, {
        "message": "签到成功", "data": {"reward_points": 5, "total_points": 130,
        "streak": 3, "checkin_date": "2026-11-14", "credential": "private-fixture"},
    }, assertions={"content": True}, step=2)
    result = trial.payload(True, None, "trial passed", template_log="无关肩带\\r\\n当前积分：130\\r\\nuser=private-fixture")
    assert [item["evidence"] for item in result["steps"]] == ["rule", "confirmed"]
    assert result["receipt"]["outcome"] == "signed_in"
    assert result["receipt"]["details"] == [
        {"label": "本次获得积分", "value": "5"},
        {"label": "总积分", "value": "130"},
        {"label": "连续签到天数", "value": "3"},
        {"label": "签到日期", "value": "2026-11-14"},
    ]
    assert result["template_log"] == [{"label": "当前积分", "value": "130"}]
    assert "private-fixture" not in json.dumps(result)


@pytest.mark.parametrize("body,code,path,mime", [
    ({"success": True}, 200, "/api/checkin", "application/json"),
    ({"message": "签到成功"}, 403, "/api/checkin", "application/json"),
    ({"message": "签到成功", "checked_in": False}, 200, "/api/checkin", "application/json"),
    ({"message": "已经签到"}, 200, "/api/activity", "application/json"),
    ("<p>签到成功</p>", 200, "/api/checkin", "text/html"),
    ({"message": "签到成功，完整敏感文字"}, 200, "/api/checkin", "application/json"),
    ({"signed_in": True}, 200, "/api/signin", "application/json"),
])
def test_trial_does_not_treat_ambiguous_evidence_as_confirmed(body, code, path, mime):
    trial = SigninTrial()
    observe(trial, body, code=code, path=path, mime=mime)
    assert trial.payload(True, None, "", template_log="签到成功")["receipt"]["outcome"] == "unconfirmed"


def test_trial_retains_last_failed_event_and_distinguishes_already_signed():
    trial = SigninTrial()
    observe(trial, {"message": "今天已签到"}, step=2)
    for _ in range(60):
        observe(trial, {"status": 0})
    observe(trial, {"message": "签到失败"}, code=409, success=False, assertions={"failed": True}, step=3)
    reply = trial.payload(False, 3, "")
    assert reply["steps"][0]["evidence"] == "confirmed"
    assert reply["receipt"]["outcome"] == "failed"
    assert reply["omitted_steps"] == 12
    assert reply["steps"][-1]["ordinal"] == 62
    assert reply["steps"][-1]["outcome"] == "failed"


def test_public_forum_route_preserves_explicit_receipt():
    trial = SigninTrial()
    observe(
        trial, {"message": "签到成功", "reward_points": 2},
        path="/plugin.php?id=attendance%3Adaily&action=sign",
    )
    assert trial.payload(True, None, "")["receipt"]["details"] == [
        {"label": "本次获得积分", "value": "2"}
    ]
