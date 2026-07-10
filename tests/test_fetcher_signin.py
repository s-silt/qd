#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""libs.fetcher 签到判定 + SSRF + 健壮性 单测。

覆盖审计问题:
- [A] run_rule success 默认 True / 错误响应误判成功 / status 断言 OR 误判
- [D #38] _proxy 注入导致 SSRF
- [D #7]  validate_cert 写死 False
- [P1 #35] 解码失败 (decode 返回 None) 导致 re.search(None) TypeError
- [P2 #37] 重试日志对 HTTPResponse 调 .replace 抛 AttributeError 被吞
"""

import logging
from io import BytesIO

import pytest
from tornado.httpclient import HTTPError, HTTPRequest, HTTPResponse
from tornado.httputil import HTTPHeaders

import libs.fetcher as fetcher_mod
from libs.fetcher import Fetcher


def make_response(body=b"", code=200, headers=None, reason="OK", error=False):
    req = HTTPRequest(url="http://example.com/")
    if headers is None:
        headers = {"Content-Type": "text/html; charset=utf-8"}
    h = HTTPHeaders(headers)
    resp = HTTPResponse(
        request=req, code=code, headers=h, buffer=BytesIO(body), reason=reason
    )
    if error is True and resp.error is None:
        resp.error = HTTPError(code, message=reason, response=resp)
    elif error is False and 200 <= code < 300:
        resp.error = None
    return resp


def base_env():
    return {"session": [], "variables": {}}


# ---------------------------------------------------------------------------
# [A] run_rule 判定
# ---------------------------------------------------------------------------


def test_ok_response_no_asserts_is_success():
    """200 + 无断言 -> 成功 (向后兼容: 正常无断言模板仍判成功)。"""
    f = Fetcher()
    resp = make_response(b"hello", code=200)
    success, _ = f.run_rule(resp, {}, base_env())
    assert success is True


def test_redirect_no_asserts_is_success():
    """302 (tornado 会设 error) + 无断言 -> 仍判成功, 不被误杀。"""
    f = Fetcher()
    resp = make_response(b"", code=302, reason="Found")
    success, _ = f.run_rule(resp, {}, base_env())
    assert success is True


def test_5xx_no_success_assert_is_failure():
    """500 + 无成功断言 -> 强制失败 (旧逻辑会默认成功)。"""
    f = Fetcher()
    resp = make_response(b"Internal Server Error", code=500, reason="ERR")
    success, msg = f.run_rule(resp, {}, base_env())
    assert success is False
    assert msg


def test_connection_error_response_is_failure():
    """连接错误合成响应 (code 599) + 无成功断言 -> 失败。"""
    f = Fetcher()
    resp = make_response(b"timeout", code=599, reason="Timeout", error=True)
    success, _ = f.run_rule(resp, {}, base_env())
    assert success is False


def test_5xx_with_matching_status_assert_can_succeed():
    """显式断言 status=500 命中 -> 用户明确意图, 判成功 (向后兼容)。"""
    f = Fetcher()
    resp = make_response(b"x", code=500, reason="ERR")
    rule = {"success_asserts": [{"re": "500", "from": "status"}]}
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is True


def test_content_success_assert_match():
    f = Fetcher()
    resp = make_response("签到成功".encode("utf-8"), code=200)
    rule = {"success_asserts": [{"re": "签到成功", "from": "content"}]}
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is True


def test_content_success_assert_no_match_is_failure():
    f = Fetcher()
    resp = make_response("出错了".encode("utf-8"), code=200)
    rule = {"success_asserts": [{"re": "签到成功", "from": "content"}]}
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is False


def test_status_only_assert_200_success_backward_compat():
    f = Fetcher()
    resp = make_response(b"whatever", code=200)
    rule = {"success_asserts": [{"re": "200", "from": "status"}]}
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is True


def test_status_and_content_assert_and_semantics():
    """[A] status:200 命中但 content 不命中 -> 不能因 OR 而误判成功 (取 AND)。"""
    f = Fetcher()
    resp = make_response("错误页面".encode("utf-8"), code=200)
    rule = {
        "success_asserts": [
            {"re": "200", "from": "status"},
            {"re": "签到成功", "from": "content"},
        ]
    }
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is False


def test_status_and_content_assert_both_match_success():
    f = Fetcher()
    resp = make_response("签到成功".encode("utf-8"), code=200)
    rule = {
        "success_asserts": [
            {"re": "200", "from": "status"},
            {"re": "签到成功", "from": "content"},
        ]
    }
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is True


def test_failed_assert_triggers_failure():
    f = Fetcher()
    resp = make_response("请先登录".encode("utf-8"), code=200)
    rule = {"failed_asserts": [{"re": "请先登录", "from": "content"}]}
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is False


# ---------------------------------------------------------------------------
# 断言引擎健壮性 (reliability 重构)
# ---------------------------------------------------------------------------


def test_eval_assert_bad_jinja_does_not_raise():
    """成功断言 re 里的 Jinja 报错必须被兜住(永不抛), 不能逃逸 run_rule。

    旧实现把 _render 放在 try 外 -> 抛 HTTPError(500) 逃逸 -> 把本已成功的签到误判为硬失败/崩溃。
    """
    f = Fetcher()
    resp = make_response("签到成功".encode("utf-8"), code=200)
    rule = {"success_asserts": [{"re": "{{ 1 // 0 }}", "from": "content"}]}
    # 不抛异常; 断言渲染失败 -> 视作未命中 -> 无正向证据 -> 判失败(而非崩溃)
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is False


def test_assert_slash_literal_semantics_preserved():
    """断言侧保持与 master 一致的字面量语义: /login/ 按字面匹配, 不被重解释为正则。

    (extract_variables 才解析 /.../flags; 断言侧刻意不解析, 避免静默放宽存量斜杠包裹断言。)
    """
    f = Fetcher()
    resp = make_response(b"loginToken=abc", code=200)
    # failed_assert '/login/' 在正文里找不到字面 '/login/' -> 不触发 -> 成功保持
    rule = {"failed_asserts": [{"re": "/login/", "from": "content"}]}
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is True


def test_assert_missing_from_defaults_to_content():
    """断言缺省 from 按 content 求值, 而非静默按空串(旧: from 缺失/拼错 -> 断言永不命中)。"""
    f = Fetcher()
    resp = make_response("签到成功".encode("utf-8"), code=200)
    rule = {"success_asserts": [{"re": "签到成功"}]}  # 无 from
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is True


def test_extract_error_not_poisoned_into_variable():
    """提取正则报错时变量不得被写成异常字符串(旧 =str(e) 会把报错当 token 发给下游请求)。"""
    f = Fetcher()
    env = base_env()
    resp = make_response(b"whatever", code=200)
    rule = {"extract_variables": [{"name": "tok", "re": "(", "from": "content"}]}  # 非法正则
    f.run_rule(resp, rule, env)
    val = env["variables"].get("tok")
    assert not val
    assert "unterminated" not in str(val)


def test_require_success_assert_strict_fails_status_only(monkeypatch):
    """开启 REQUIRE_SUCCESS_ASSERT 后, 仅状态断言(无内容成功断言)的"成功"判失败。"""
    f = Fetcher()
    monkeypatch.setattr(fetcher_mod.config, "require_success_assert", True)
    resp = make_response(b"login page", code=200)
    rule = {"success_asserts": [{"re": "200", "from": "status"}]}
    success, msg = f.run_rule(resp, rule, base_env())
    assert success is False
    assert msg


@pytest.mark.asyncio
async def test_zero_request_top_level_fails(monkeypatch):
    """for 循环变量为空 -> 整模板 0 次请求 -> (守卫开启时)判失败, 避免"空跑"误报签到成功。"""
    f = Fetcher()
    monkeypatch.setattr(fetcher_mod.config, "fail_on_zero_request", True)  # 守卫默认 opt-in, 测试显式开启
    tpl = [
        {"request": {"method": "GET", "url": "{% for x in items %}", "headers": [], "cookies": []}, "rule": {}},
        {"request": {"method": "GET", "url": "http://ex/", "headers": [], "cookies": []}, "rule": {}},
        {"request": {"method": "GET", "url": "{% endfor %}", "headers": [], "cookies": []}, "rule": {}},
    ]
    env = {"variables": {}, "session": []}  # items 未定义 -> 空可迭代 -> 循环体 0 次
    with pytest.raises(Exception):
        await f.do_fetch(tpl, env, proxies=[], request_limit=50)


# ---------------------------------------------------------------------------
# [P1 #35] 解码失败不抛 TypeError
# ---------------------------------------------------------------------------


def test_decode_failure_does_not_raise(monkeypatch):
    """utils.decode 返回 None 时, re.search 不应抛 TypeError 被误报为请求失败。"""
    f = Fetcher()
    monkeypatch.setattr(fetcher_mod.utils, "decode", lambda *a, **k: None)
    resp = make_response(b"\xff\xfe garbage", code=200)
    rule = {"success_asserts": [{"re": "签到成功", "from": "content"}]}
    # 不应抛异常
    success, _ = f.run_rule(resp, rule, base_env())
    assert success is False  # 无法解码 + 断言不命中 -> 失败, 而非崩溃


def test_decode_failure_failed_assert_safe(monkeypatch):
    f = Fetcher()
    monkeypatch.setattr(fetcher_mod.utils, "decode", lambda *a, **k: None)
    resp = make_response(b"\xff\xfe", code=200)
    rule = {"failed_asserts": [{"re": "错误", "from": "content"}]}
    success, _ = f.run_rule(resp, rule, base_env())
    # decode 失败回退空串, 断言不命中, 200 无错误 -> 成功 (不崩溃)
    assert success is True


# ---------------------------------------------------------------------------
# [D #7] validate_cert
# ---------------------------------------------------------------------------


def _cert_obj(url="https://example.com/", extra=None):
    request = {
        "method": "GET",
        "url": url,
        "headers": [],
        "cookies": [],
        "data": "",
    }
    if extra:
        request.update(extra)
    return {"request": request, "rule": {}, "env": {"variables": {}, "session": []}}


def test_validate_cert_default_true():
    f = Fetcher()
    req, _, _ = f.build_request(_cert_obj())
    assert req.validate_cert is True


def test_validate_cert_global_override(monkeypatch):
    f = Fetcher()
    monkeypatch.setattr(fetcher_mod, "DEFAULT_VALIDATE_CERT", False)
    req, _, _ = f.build_request(_cert_obj())
    assert req.validate_cert is False


def test_validate_cert_per_request_override():
    f = Fetcher()
    req, _, _ = f.build_request(_cert_obj(extra={"validate_cert": False}))
    assert req.validate_cert is False


# ---------------------------------------------------------------------------
# [D #38] _proxy SSRF 防护
# ---------------------------------------------------------------------------


def _proxy_tpl():
    return [
        {
            "request": {
                "method": "GET",
                "url": "http://target.example.com/",
                "headers": [],
                "cookies": [],
            },
            "rule": {},
        }
    ]


@pytest.mark.asyncio
async def test_injected_proxy_blocked(monkeypatch):
    """注入的 _proxy 指向内网/受限地址时应被拦截并清空, 不导流量。"""
    f = Fetcher()
    captured = {}

    async def fake_fetch(obj, proxy=None):
        captured["proxy"] = proxy
        return {"success": True, "response": make_response(b"ok"), "env": obj["env"], "msg": ""}

    monkeypatch.setattr(f, "fetch", fake_fetch)
    monkeypatch.setattr(
        fetcher_mod, "resolve_blocked_reason", lambda host: "目标地址属于受限网段"
    )

    env = {"variables": {"_proxy": "http://169.254.169.254:80"}, "session": []}
    await f.do_fetch(_proxy_tpl(), env, proxies=[], request_limit=10)
    assert captured["proxy"] == {}
    assert env["variables"]["_proxy"] == ""


@pytest.mark.asyncio
async def test_injected_proxy_bad_scheme_blocked(monkeypatch):
    f = Fetcher()
    captured = {}

    async def fake_fetch(obj, proxy=None):
        captured["proxy"] = proxy
        return {"success": True, "response": make_response(b"ok"), "env": obj["env"], "msg": ""}

    monkeypatch.setattr(f, "fetch", fake_fetch)
    # 即使地址放行, 非代理 scheme 也应拦截
    monkeypatch.setattr(fetcher_mod, "resolve_blocked_reason", lambda host: "")

    env = {"variables": {"_proxy": "file://etc/passwd"}, "session": []}
    await f.do_fetch(_proxy_tpl(), env, proxies=[], request_limit=10)
    assert captured["proxy"] == {}


@pytest.mark.asyncio
async def test_valid_proxy_allowed(monkeypatch):
    f = Fetcher()
    captured = {}

    async def fake_fetch(obj, proxy=None):
        captured["proxy"] = proxy
        return {"success": True, "response": make_response(b"ok"), "env": obj["env"], "msg": ""}

    monkeypatch.setattr(f, "fetch", fake_fetch)
    monkeypatch.setattr(fetcher_mod, "resolve_blocked_reason", lambda host: "")

    env = {"variables": {"_proxy": "http://goodproxy.example.com:8080"}, "session": []}
    await f.do_fetch(_proxy_tpl(), env, proxies=[], request_limit=10)
    assert captured["proxy"].get("host") == "goodproxy.example.com"
    assert captured["proxy"].get("scheme") == "http"


# ---------------------------------------------------------------------------
# [P2 #37] 重试日志 .replace AttributeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_log_no_attributeerror(monkeypatch):
    """SimpleAsyncHTTPClient 重试失败时, 诊断日志不应因对 HTTPResponse 调 .replace 而崩溃丢失。"""
    f = Fetcher()
    monkeypatch.setattr(fetcher_mod, "pycurl", object())  # 启用 retry 分支

    err = HTTPError(418, message="boom")
    err.message = ""  # 绕过构造器默认值, 强制走 e.response.replace 路径 (触发 bug)
    err.response = make_response(b"diag-body", code=418, reason="teapot")

    async def boom(req):
        raise err

    monkeypatch.setattr(f.client, "fetch", boom)

    class FakeSimple:
        async def fetch(self, req):
            raise Exception("net down")

    monkeypatch.setattr(
        fetcher_mod.simple_httpclient, "SimpleAsyncHTTPClient", lambda: FakeSimple()
    )

    calls = []
    orig_error = fetcher_mod.logger_fetcher.error

    def rec_error(*a, **k):
        calls.append(a)
        return orig_error(*a, **k)

    monkeypatch.setattr(fetcher_mod.logger_fetcher, "error", rec_error)

    obj = _cert_obj(url="http://target.example.com/")
    rule, env, resp = await f.build_response(obj)
    # 修复后: error 日志被成功调用 (参数求值不再抛 AttributeError)
    assert calls, "诊断日志未被记录 (说明 .replace 在参数求值时崩溃)"
