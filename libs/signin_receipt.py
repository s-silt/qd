"""Bounded, credential-free evidence for a single template trial."""

import json
import math
import re
import time
from collections import deque
from datetime import date
from urllib.parse import unquote, urlsplit

from libs.signin_workbench import is_public_signin_query

_MAX_BODY = 64 * 1024
_HEAD_STEPS = 40
_TAIL_STEPS = 10
_SIGNIN_PATH = re.compile(
    r"(?:^|[/_.-])(?:check[-_]?in|attendance|qiandao|daily[-_]?sign|sign[-_]?in|sign|punch|签到|打卡)(?:$|[/_.-])",
    re.I,
)
_MESSAGES = {
    "signed_in": ("签到成功", "打卡成功", "check-in successful", "checkin successful"),
    "already_signed": ("今日已签到", "今天已签到", "已经签到", "重复签到", "已签到", "already checked in"),
    "failed": ("签到失败", "打卡失败", "check-in failed", "checkin failed"),
}
_SIGNIN_STATE = ("checked_in", "signed", "is_signed")
_DETAILS = (
    ("reward_points", "本次获得积分"), ("earned_points", "本次获得积分"),
    ("points", "积分"), ("total_points", "总积分"),
    ("consecutive_days", "连续签到天数"), ("streak", "连续签到天数"),
    ("signin_days", "累计签到天数"), ("checkin_days", "累计签到天数"),
    ("checkin_date", "签到日期"), ("attendance_date", "签到日期"),
)
_PLAIN_REPLY = re.compile(
    r"(?:签到成功|打卡成功|(?:今天|今日)?已签到|已经签到|重复签到|签到失败|打卡失败|"
    r"check-?in successful|already checked in|check-?in failed)[。！!.]?", re.I,
)
_LOG_ENTRY = re.compile(
    r"(本次获得积分|本次积分|总积分|当前积分|积分|连续签到天数|累计签到天数|签到日期)"
    r"[：:]\s*(-?\d{1,9}(?:\.\d{1,2})?|(?:19|20)\d{2}-\d{2}-\d{2})\s*(?:天|分)?"
)
_NUMBER = re.compile(r"-?\d{1,9}(?:\.\d{1,2})?")
_REWARD_REPLY = re.compile(
    r"(?:签到成功|打卡成功)[。！!，,、\s]+(?:本次)?(?:获得|奖励)\s*(\d{1,9})\s*(?:积分|分)[。！!.]?"
)


def _no_receipt(message="站点响应里没有可确认的签到回执。"):
    return {"outcome": "unconfirmed", "message": message, "details": []}


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate receipt field")
    return obj


def site_signin_receipt(response, url):
    raw_status = getattr(response, "code", None)
    status = raw_status if type(raw_status) is int and 100 <= raw_status < 599 else None
    if status is None or not response.body or len(response.body) > _MAX_BODY:
        return _no_receipt()
    actual_url = getattr(response, "effective_url", "") or url
    try:
        parsed = urlsplit(actual_url)
        declared = urlsplit(url)
    except ValueError:
        return _no_receipt()
    if (
        declared.scheme not in ("http", "https")
        or parsed.scheme not in ("http", "https")
        or parsed.hostname != declared.hostname
        or not (
            _SIGNIN_PATH.search(unquote(parsed.path))
            or is_public_signin_query(parsed.path, parsed.query)
        )
    ):
        return _no_receipt()
    mime = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if mime != "application/json" and not mime.endswith("+json") and mime != "text/plain":
        return _no_receipt()
    text = response.body.decode("utf-8-sig", "replace").strip()
    if mime == "text/plain":
        if not (_PLAIN_REPLY.fullmatch(text) or _REWARD_REPLY.fullmatch(text)):
            return _no_receipt()
        containers = [{"message": text}]
    else:
        try:
            obj = json.loads(text, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            return _no_receipt()
        if not isinstance(obj, dict):
            return _no_receipt()
        nested = obj.get("data")
        containers = [obj, nested] if isinstance(nested, dict) else [obj]

    phrases = [
        value.strip().lower()
        for item in containers
        for key in ("message", "msg", "status_text")
        for value in [item.get(key)]
        if isinstance(value, str) and len(value) <= 80
    ]
    rewards = [match[1] for value in phrases if (match := _REWARD_REPLY.fullmatch(value))]
    phrases = [value.rstrip("。！!.") for value in phrases]
    state = [item[key] for item in containers for key in _SIGNIN_STATE if key in item]
    if any(value in _MESSAGES["failed"] for value in phrases):
        return {"outcome": "failed", "message": "站点返回「签到失败」。", "details": []}
    if not 200 <= status < 300 or any(value is False or type(value) is int and value == 0 for value in state):
        return _no_receipt()

    if any(value in _MESSAGES["already_signed"] for value in phrases):
        outcome, message = "already_signed", "站点返回「已签到」。"
    elif (
        (rewards or any(value in _MESSAGES["signed_in"] for value in phrases))
        and not any(item.get("success") is False for item in containers)
    ):
        outcome, message = "signed_in", "站点返回「签到成功」。"
    elif any(value is True or type(value) is int and value == 1 for value in state):
        outcome, message = "already_signed", "站点返回的签到状态为已签到。"
    else:
        return _no_receipt()

    details = {}
    for key, label in _DETAILS:
        for item in containers:
            value = item.get(key)
            if key.endswith("_date"):
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    continue
                try:
                    value = date.fromisoformat(value).isoformat()
                except ValueError:
                    continue
            elif type(value) in (int, float) and abs(value) <= 10**9 and math.isfinite(value):
                value = str(value)
            elif isinstance(value, str) and _NUMBER.fullmatch(value):
                pass
            else:
                continue
            details.setdefault(label, value)
    if outcome == "signed_in" and rewards and len(set(rewards)) == 1:
        details.setdefault("本次获得积分", rewards[0])
    return {
        "outcome": outcome, "message": message,
        "details": [{"label": label, "value": value} for label, value in details.items()],
    }


def receipt_log(receipt):
    lines = [receipt["message"]]
    lines.extend(f"{item['label']}：{item['value']}" for item in receipt["details"])
    return r"\r\n".join(lines)


class SigninTrial:
    def __init__(self):
        self.start = time.perf_counter()
        self.head = []
        self.tail = deque(maxlen=_TAIL_STEPS)
        self.count = 0
        self.receipt = _no_receipt()

    def observe(self, entry, response, success, assertions):
        raw_status = getattr(response, "code", None)
        requested_url = entry["request"]["url"]
        internal = requested_url.lstrip().startswith("api://")
        status = raw_status if not internal and type(raw_status) is int and 100 <= raw_status < 599 else None
        evidence = site_signin_receipt(response, requested_url) if response is not None else _no_receipt()
        if evidence["outcome"] != "unconfirmed":
            if self.receipt["outcome"] != "signed_in" or evidence["outcome"] == "failed":
                self.receipt = evidence
        if assertions.get("failed"):
            message = "命中失败条件。"
        elif assertions.get("content") is False or assertions.get("status") is False:
            message = "响应未通过配置的判定。"
        elif not success:
            message = "请求未完成或没有通过 QD 判定。"
        elif assertions.get("content") is True:
            message = "命中模板设置的响应判定。"
        else:
            message = "QD 已继续执行，本步没有匹配到响应判定。"
        if evidence["outcome"] != "unconfirmed":
            message += evidence["message"]

        self.count += 1
        item = {
            "step": entry["idx"], "ordinal": self.count, "status": status, "internal": internal,
            "outcome": "passed" if success else "failed",
            "evidence": (
                "confirmed" if evidence["outcome"] in ("signed_in", "already_signed")
                else "rule" if assertions.get("content") is True else "none"
            ),
            "message": message, "details": evidence["details"],
        }
        (self.head if self.count <= _HEAD_STEPS else self.tail).append(item)

    def payload(self, success, failed_step, message, *, template_log=None):
        log_details = {}
        if isinstance(template_log, str) and len(template_log) <= 4096:
            for line in template_log.replace("\\r\\n", "\n").splitlines():
                value = line.strip()
                match = _LOG_ENTRY.fullmatch(value)
                if match:
                    label, recorded = match.groups()
                    log_details.setdefault(label, recorded)
                elif _PLAIN_REPLY.fullmatch(value):
                    log_details.setdefault("模板消息", value)
        return {
            "success": success, "failed_step": failed_step, "message": message,
            "steps": self.head + list(self.tail), "receipt": self.receipt,
            "omitted_steps": max(0, self.count - len(self.head) - len(self.tail)),
            "duration_ms": round((time.perf_counter() - self.start) * 1000),
            "template_log": [{"label": label, "value": value} for label, value in log_details.items()],
        }
