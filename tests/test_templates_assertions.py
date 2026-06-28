#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""开箱即用模板 (templates/*.json) 的断言/抽取正则行为单测。

通过【真实】Fetcher.run_rule 把合成响应喂进模板自带的 rule, 验证签到判定行为,
而不是只检查字符串是否存在。覆盖审计问题:

- [#29] linkai 第2步: "code":200 / "success":true 信封级判据把业务失败当成功。
- [#41] right-enshan 第2步: 裸 <html / <!DOCTYPE html 作 failed_assert 误杀 Discuz 成功页。
- [#42] right-enshan 第1步: formhash 的 success_assert 与 extract 正则不一致 (仅 logout 链接含 formhash 时抽取失败)。
- [#43] right-enshan 第2步: 裸 "签到过" 命中 "还没签到过"。
- [#44] linkai: 明文回传账号密码 -> README/模板脱敏说明。

模板可被框架正常导入 (json.load 合法) 也一并验证。
"""

import json
import os
from io import BytesIO

from tornado.httpclient import HTTPRequest, HTTPResponse
from tornado.httputil import HTTPHeaders

from libs.fetcher import Fetcher

TEMPLATES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "templates")
)


def load_template(name):
    with open(os.path.join(TEMPLATES_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def make_response(body, code=200, reason="OK"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    req = HTTPRequest(url="http://example.com/")
    h = HTTPHeaders({"Content-Type": "text/html; charset=utf-8"})
    resp = HTTPResponse(request=req, code=code, headers=h, buffer=BytesIO(body), reason=reason)
    if 200 <= code < 300:
        resp.error = None
    return resp


def base_env():
    return {"session": [], "variables": {}}


def run(rule, body, code=200):
    """跑真实 run_rule, 返回 (success, msg, env)。"""
    f = Fetcher()
    env = base_env()
    success, msg = f.run_rule(make_response(body, code=code), rule, env)
    return success, msg, env


# ---------------------------------------------------------------------------
# 通用: JSON 合法 (可被框架导入)
# ---------------------------------------------------------------------------


def test_all_templates_valid_json():
    for name in ("nodeseek-signin.json", "right-enshan-signin.json", "linkai-signin.json"):
        tpl = load_template(name)
        assert isinstance(tpl, list) and tpl
        for step in tpl:
            assert "request" in step and "rule" in step


# ---------------------------------------------------------------------------
# [#29] linkai 第2步: 信封级判据不得把业务失败当成功
# ---------------------------------------------------------------------------


def linkai_sign_rule():
    return load_template("linkai-signin.json")[1]["rule"]


def test_linkai_envelope_code200_business_fail_is_failure():
    """{"code":200,"success":true,...} 但业务失败 -> 不得判成功 (旧 envelope 判据会误判)。"""
    rule = linkai_sign_rule()
    body = '{"code":200,"success":true,"message":"系统繁忙，请稍后再试"}'
    success, _, _ = run(rule, body)
    assert success is False


def test_linkai_first_success_is_success():
    rule = linkai_sign_rule()
    body = '{"code":200,"success":true,"message":"签到成功，获得 5 积分"}'
    success, _, _ = run(rule, body)
    assert success is True


def test_linkai_already_signed_is_success_even_if_success_false():
    """文档承诺 "今日已签到" 算成功; 即便 envelope success:false 也不得被误杀。"""
    rule = linkai_sign_rule()
    body = '{"code":200,"success":false,"message":"今日已签到，请明天再来"}'
    success, _, _ = run(rule, body)
    assert success is True


def test_linkai_genuine_failure_is_failure():
    rule = linkai_sign_rule()
    for msg in ("操作太频繁", "今日已达上限", "签到失败"):
        body = '{"code":200,"success":false,"message":"%s"}' % msg
        success, _, _ = run(rule, body)
        assert success is False, msg


def test_linkai_success_asserts_drop_envelope_tokens():
    rule = linkai_sign_rule()
    blob = json.dumps(rule["success_asserts"], ensure_ascii=False)
    assert '"code"' not in blob
    assert "success" not in blob  # 不再用 "success":true 作成功证据


# ---------------------------------------------------------------------------
# [#42] right-enshan 第1步: formhash success_assert 与 extract 一致
# ---------------------------------------------------------------------------


def enshan_step1_rule():
    return load_template("right-enshan-signin.json")[0]["rule"]


def test_enshan_formhash_hidden_input_extracts():
    rule = enshan_step1_rule()
    body = '<input type="hidden" name="formhash" value="ab12cd34" />'
    success, _, env = run(rule, body)
    assert success is True
    assert env["variables"].get("formhash") == "ab12cd34"


def test_enshan_formhash_logout_link_only_extracts():
    """页面只在 logout 链接里带 formhash (无隐藏域) 时, success 命中则必须能抽到 formhash (#42)。"""
    rule = enshan_step1_rule()
    body = '<a href="member.php?mod=logging&amp;action=logout&amp;formhash=deadbeef">退出</a>'
    success, _, env = run(rule, body)
    assert success is True
    assert env["variables"].get("formhash") == "deadbeef"


def test_enshan_not_logged_in_fails_step1():
    rule = enshan_step1_rule()
    body = '<form><input name="loginhash" value="Lxxxx">您需要先登录</form>'
    success, _, env = run(rule, body)
    assert success is False


# ---------------------------------------------------------------------------
# [#41][#43] right-enshan 第2步
# ---------------------------------------------------------------------------


def enshan_sign_rule():
    return load_template("right-enshan-signin.json")[1]["rule"]


def test_enshan_not_yet_signed_phrase_not_success():
    """裸 "签到过" 不得命中 "还没签到过" (#43)。"""
    rule = enshan_sign_rule()
    body = "您还没签到过，请先完成今日签到"
    success, _, _ = run(rule, body)
    assert success is False


def test_enshan_already_signed_is_success():
    rule = enshan_sign_rule()
    body = "您今天已经签到过了"
    success, _, _ = run(rule, body)
    assert success is True


def test_enshan_json_success_is_success():
    rule = enshan_sign_rule()
    body = '{"success":true,"messageval":"签到成功","message":"今日签到成功"}'
    success, _, _ = run(rule, body)
    assert success is True


def test_enshan_html_success_page_not_failed_by_bare_html():
    """Discuz 成功页本就是 HTML, 裸 <html/<!DOCTYPE html 不得把它判失败 (#41)。"""
    rule = enshan_sign_rule()
    body = "<!DOCTYPE html><html><body>签到成功，恭喜您获得恩山币</body></html>"
    success, _, _ = run(rule, body)
    assert success is True


def test_enshan_login_page_returned_is_failure():
    """cookie 失效返回登录页 -> 失败 (登录页特征短语, 而非裸 <html)。"""
    rule = enshan_sign_rule()
    body = '<!DOCTYPE html><html><form><input name="loginhash"><input name="loginsubmit"></form></html>'
    success, _, _ = run(rule, body)
    assert success is False


def test_enshan_failed_asserts_drop_bare_html():
    rule = enshan_sign_rule()
    blob = json.dumps(rule["failed_asserts"], ensure_ascii=False)
    assert "<html" not in blob
    assert "<!DOCTYPE html" not in blob


# ---------------------------------------------------------------------------
# [#44] 明文凭据脱敏说明
# ---------------------------------------------------------------------------


def test_readme_mentions_password_token_guidance():
    with open(os.path.join(TEMPLATES_DIR, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    assert "password" in readme
    assert "token" in readme.lower()
    # 不应内置真实凭据: 模板里只能是占位符
    linkai = load_template("linkai-signin.json")
    data = linkai[0]["request"]["data"]
    assert "{{password" in data and "{{username" in data


def test_linkai_template_comment_has_credential_note():
    linkai = load_template("linkai-signin.json")
    comment = linkai[0].get("comment", "")
    assert "密码" in comment
