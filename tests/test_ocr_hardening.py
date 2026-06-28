# -*- coding: utf-8 -*-
"""OCR 健壮性加固回归测试 (文件组 ocr)。

覆盖审计问题:
    #26  onnxruntime 子进程探测 → 不可用机自动降级禁用 (主进程不被 SIGILL 杀死)
    #27  onnx 同步推理放 run_in_executor, 不阻塞 Tornado IOLoop
    #28  extra 模型 charsets 用配对的 extra_charsets_name; 单模型失败隔离
    #40  OCR 不可用时 406 不得被吞成 200
    D#8/#9 get_img_from_url 过 SSRF 守卫 / 禁重定向 / 去掉 verify_ssl=False

加载方式复用 test_ddddocr_lazy 的打桩思路, 只加载真实 util.py 与 libs.security,
避开 umsgpack / sqlalchemy 等重依赖。
"""
import asyncio
import base64
import importlib.util
import logging
import os
import sys
import types

import pytest
from tornado.web import HTTPError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _install_web_handlers_base_stub():
    web_pkg = sys.modules.setdefault("web", types.ModuleType("web"))
    web_pkg.__path__ = []
    handlers_pkg = sys.modules.setdefault(
        "web.handlers", types.ModuleType("web.handlers")
    )
    handlers_pkg.__path__ = []
    base_stub = types.ModuleType("web.handlers.base")

    class _BaseHandler:
        pass

    base_stub.BaseHandler = _BaseHandler
    base_stub.logger_web_handler = logging.getLogger("test.web.handlers.base")
    sys.modules["web.handlers.base"] = base_stub


def _load_util():
    _install_web_handlers_base_stub()
    path = os.path.join(REPO_ROOT, "web", "handlers", "util.py")
    spec = importlib.util.spec_from_file_location("qd_util_ocr_hardening", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def util():
    mod = _load_util()
    mod._ddddocr_singleton = None
    mod._ddddocr_init_failed = False
    mod._ddddocr_probe_ok = None
    yield mod


# ----------------------------- #28 extra 模型 -----------------------------

class _RecordingDdddOcr:
    """记录构造参数; import_onnx_path 含 'bad' 时构造失败, 模拟坏模型。"""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        path = kwargs.get("import_onnx_path", "")
        if "bad" in path:
            raise RuntimeError("simulated broken extra onnx model")


def test_extra_charsets_uses_charsets_name(util, monkeypatch):
    """#28: charsets_path 必须用配对的 extra_charsets_name, 而非 onnx_name。"""
    monkeypatch.setattr(
        util, "ddddocr", types.SimpleNamespace(DdddOcr=_RecordingDdddOcr)
    )
    monkeypatch.setattr(util.config, "extra_onnx_name", ["good1"])
    monkeypatch.setattr(util.config, "extra_charsets_name", ["cs1"])

    server = util.DdddOcrServer()
    assert "good1" in server.extra
    kw = server.extra["good1"].kwargs
    assert kw["import_onnx_path"].replace("\\", "/").endswith("config/good1.onnx")
    # 关键: charsets 用 cs1.json 而不是 good1.json
    assert kw["charsets_path"].replace("\\", "/").endswith("config/cs1.json")


def test_extra_model_failure_isolated(util, monkeypatch):
    """#28: 单个 extra 模型坏掉不得连坐其它模型 / 整个 OCR。"""
    monkeypatch.setattr(
        util, "ddddocr", types.SimpleNamespace(DdddOcr=_RecordingDdddOcr)
    )
    monkeypatch.setattr(util.config, "extra_onnx_name", ["good1", "bad", "good2"])
    monkeypatch.setattr(util.config, "extra_charsets_name", ["cs1", "cs2", "cs3"])

    server = util.DdddOcrServer()  # 不应抛异常
    assert "good1" in server.extra
    assert "good2" in server.extra
    assert "bad" not in server.extra


# ----------------------------- #26 子进程探测 -----------------------------

def test_probe_failure_disables_ocr(util, monkeypatch):
    """#26: onnxruntime 子进程自检失败 → 自动降级禁用, 返回 None。"""
    monkeypatch.setattr(
        util, "ddddocr", types.SimpleNamespace(DdddOcr=_RecordingDdddOcr)
    )
    monkeypatch.setattr(util.config, "enable_ddddocr", True)
    monkeypatch.setattr(util, "_probe_onnxruntime_available", lambda: False)

    assert util.get_ddddocr_server() is None
    assert util._ddddocr_init_failed is True


def test_probe_success_allows_ocr(util, monkeypatch):
    """#26: 自检通过 → 正常构建单例。"""
    monkeypatch.setattr(
        util, "ddddocr", types.SimpleNamespace(DdddOcr=_RecordingDdddOcr)
    )
    monkeypatch.setattr(util.config, "enable_ddddocr", True)
    monkeypatch.setattr(util.config, "extra_onnx_name", [""])
    monkeypatch.setattr(util.config, "extra_charsets_name", [""])
    monkeypatch.setattr(util, "_probe_onnxruntime_available", lambda: True)

    assert util.get_ddddocr_server() is not None


def test_probe_returncode_mapping(util, monkeypatch):
    """#26: 子进程 returncode!=0 → 探测返回 False; ==0 → True; 结果带缓存。"""
    calls = {"n": 0}

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc
            self.stderr = b""

    def fake_run_fail(*a, **k):
        calls["n"] += 1
        return _Proc(132)  # 128+SIGILL(4)

    util._ddddocr_probe_ok = None
    monkeypatch.setattr(util.subprocess, "run", fake_run_fail)
    assert util._probe_onnxruntime_available() is False
    # 缓存: 第二次不再起子进程
    assert util._probe_onnxruntime_available() is False
    assert calls["n"] == 1

    def fake_run_ok(*a, **k):
        return _Proc(0)

    util._ddddocr_probe_ok = None
    monkeypatch.setattr(util.subprocess, "run", fake_run_ok)
    assert util._probe_onnxruntime_available() is True


# ----------------------------- D#8/#9 SSRF -----------------------------

def test_get_img_from_url_blocks_ssrf(util):
    """D#8: 指向云元数据/环回等受限地址必须在发请求前被拦截。"""
    with pytest.raises(HTTPError) as ei:
        asyncio.run(util.get_img_from_url("http://169.254.169.254/latest/meta-data/"))
    assert ei.value.status_code == 403


def test_get_img_from_url_rejects_bad_scheme(util):
    """D#8: 非 http/https scheme 拒绝(防 file:// 等)。"""
    with pytest.raises(HTTPError) as ei:
        asyncio.run(util.get_img_from_url("file:///etc/passwd"))
    assert ei.value.status_code in (400, 403)


class _FakeResp:
    def __init__(self, status, body=b"img"):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self._body


class _FakeSession:
    last_get_kwargs = None

    def __init__(self, **kwargs):
        self._status = _FakeSession._next_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kwargs):
        _FakeSession.last_get_kwargs = kwargs
        return _FakeResp(self._status)


def _install_fake_aiohttp(util, status):
    _FakeSession._next_status = status
    _FakeSession.last_get_kwargs = None
    fake = types.SimpleNamespace(ClientSession=lambda **k: _FakeSession(**k))
    util.aiohttp = fake


def test_get_img_from_url_no_verify_ssl_and_no_redirect(util):
    """D#8/#9: 不得传 verify_ssl=False; 必须禁用重定向; 成功路径正常返回。"""
    _install_fake_aiohttp(util, 200)
    out = asyncio.run(util.get_img_from_url("http://93.184.216.34/x.png"))
    assert out == b"img"
    kw = _FakeSession.last_get_kwargs
    assert "verify_ssl" not in kw  # 不再禁用证书校验
    assert kw.get("allow_redirects") is False  # 禁重定向


def test_get_img_from_url_redirect_rejected(util):
    """D#9: 服务器返回 3xx 重定向 → 拒绝(防绕过 SSRF 守卫)。"""
    _install_fake_aiohttp(util, 302)
    with pytest.raises(HTTPError) as ei:
        asyncio.run(util.get_img_from_url("http://93.184.216.34/x.png"))
    assert ei.value.status_code == 403


# ----------------------------- #27 run_in_executor -----------------------------

def test_run_in_executor_helper(util):
    """#27: 阻塞函数经线程池执行, 返回结果。"""

    def blocking(a, b=0):
        return a + b

    out = asyncio.run(util._run_in_executor(blocking, 2, b=3))
    assert out == 5


# ----------------------------- #40 406 不被吞 -----------------------------

def _make_handler(util, cls, monkeypatch, server):
    h = cls.__new__(cls)
    h.current_user = {"id": 1, "isadmin": False}
    h._body = {}
    h.set_header = lambda *a, **k: None
    h.write = lambda s: h._body.__setitem__("body", s)
    h.set_status = lambda *a, **k: None
    h.evil = lambda *a, **k: None
    h.request = types.SimpleNamespace(headers={}, body=b"")
    args = {}
    h.get_argument = lambda name, default=None: args.get(name, default)
    h._args = args
    monkeypatch.setattr(util, "get_ddddocr_server", lambda: server)
    return h


def test_ocr_handler_unavailable_raises_406(util, monkeypatch):
    """#40: OCR 不可用时 handler 必须抛 HTTPError(406), 不能写成 200 OK。"""
    h = _make_handler(util, util.DdddOcrHandler, monkeypatch, None)
    with pytest.raises(HTTPError) as ei:
        asyncio.run(h.get())
    assert ei.value.status_code == 406
    # 不得已写出伪造的 200 结果
    assert h._body.get("body") is None


def test_ocr_handler_available_returns_result(util, monkeypatch):
    """可用时正常返回 Result(并走 executor 路径)。"""

    class _Server:
        def classification(self, img, old=False, extra_onnx_name=""):
            return "abcd"

    h = _make_handler(util, util.DdddOcrHandler, monkeypatch, _Server())
    h._args["img"] = base64.b64encode(b"x").decode()
    asyncio.run(h.get())
    import json

    body = json.loads(h._body["body"])
    assert body["Result"] == "abcd"
    assert body["状态"] == "OK"
