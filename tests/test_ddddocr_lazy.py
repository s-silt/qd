# -*- coding: utf-8 -*-
"""回归测试: 验证码 OCR(ddddocr)惰性初始化的优雅降级行为。

防止回归到旧 bug:
    web/handlers/util.py 在【模块 import 时】就执行 `DdddOcrServer()`, 其 __init__ 会
    同步加载 4 个 onnx 模型。onnxruntime 在缺少 AVX/AVX2 指令集的 CPU 上加载模型会直接
    触发 SIGILL(Illegal instruction, core dumped), 整个进程在启动 import 阶段就死掉,
    配合 docker `restart: unless-stopped` 表现为容器秒退 / 反复重启。

修复要点(本测试逐条覆盖):
    1. 模块 import 不再触碰 onnxruntime —— import 后 OCR 单例必须仍为空(惰性);
    2. 真正取用时若初始化抛【Python 级】异常 → 降级返回 None, 不向上抛, 不拖垮服务;
    3. ENABLE_DDDDOCR=false → 永不构建, 直接返回 None;
    4. 初始化成功 → 返回单例, 且重复取用返回同一对象(带缓存)。

注: 真正的 SIGILL 是原生崩溃, Python try/except 拦不住 —— 它由「惰性加载(启动不碰
onnxruntime)」这一设计本身规避, 无法也无需在单元测试里复现。本测试覆盖的是可被
try/except 兜住的 Python 级失败路径, 以及「import 不再 eager 初始化」这一核心回归点。

这里用打桩方式只加载 util.py 与 get_ddddocr_server 相关的真实代码, 避开
umsgpack / sqlalchemy 等重依赖(它们与本测试无关)。
"""
import importlib.util
import logging
import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _install_web_handlers_base_stub():
    """打桩 web.handlers.base, 避免触发其 umsgpack / db 等重依赖。"""
    web_pkg = sys.modules.setdefault("web", types.ModuleType("web"))
    web_pkg.__path__ = []  # 标记为 package, 阻止 Python 去跑真实 __init__
    handlers_pkg = sys.modules.setdefault(
        "web.handlers", types.ModuleType("web.handlers")
    )
    handlers_pkg.__path__ = []
    base_stub = types.ModuleType("web.handlers.base")

    class _BaseHandler:  # 仅作占位基类, 满足 util.py 里 handler 类定义
        pass

    base_stub.BaseHandler = _BaseHandler
    base_stub.logger_web_handler = logging.getLogger("test.web.handlers.base")
    sys.modules["web.handlers.base"] = base_stub


def _load_util():
    """以独立模块名加载真实的 web/handlers/util.py。"""
    _install_web_handlers_base_stub()
    path = os.path.join(REPO_ROOT, "web", "handlers", "util.py")
    spec = importlib.util.spec_from_file_location("qd_util_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDdddOcr:
    """构造成功的假 DdddOcr。"""

    def __init__(self, *args, **kwargs):
        pass


class _BoomDdddOcr:
    """构造即抛异常, 模拟 onnxruntime 加载失败的【Python 级】路径。"""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("simulated onnxruntime init failure")


@pytest.fixture()
def util():
    mod = _load_util()
    # 每个用例前重置惰性缓存, 保证用例间互不影响
    mod._ddddocr_singleton = None
    mod._ddddocr_init_failed = False
    # 这些用例验证的是「构建路径」, 假定 onnxruntime 本机可用; 直接放行子进程自检
    # (#26), 避免在无 ddddocr 的 CI 上误把自检判失败而改变这些用例语义。
    mod._ddddocr_probe_ok = True
    yield mod


def test_import_does_not_eagerly_initialize(util):
    """核心回归点: 仅 import 模块不得构建 OCR 单例(否则启动期就会加载 onnx → 崩)。"""
    assert util._ddddocr_singleton is None


def test_returns_none_when_ddddocr_missing(util, monkeypatch):
    """ddddocr 未安装(如非 x86_64/arm64 架构)→ 直接降级 None。"""
    monkeypatch.setattr(util, "ddddocr", None, raising=False)
    assert util.get_ddddocr_server() is None


def test_disabled_by_env_returns_none(util, monkeypatch):
    """ENABLE_DDDDOCR=false → 即便 ddddocr 可用也永不构建。"""
    monkeypatch.setattr(util, "ddddocr", types.SimpleNamespace(DdddOcr=_FakeDdddOcr))
    monkeypatch.setattr(util.config, "enable_ddddocr", False)
    assert util.get_ddddocr_server() is None


def test_init_failure_degrades_gracefully(util, monkeypatch):
    """初始化抛 Python 异常 → 降级 None, 不抛出, 并标记 init_failed 避免反复重试。"""
    monkeypatch.setattr(util, "ddddocr", types.SimpleNamespace(DdddOcr=_BoomDdddOcr))
    monkeypatch.setattr(util.config, "enable_ddddocr", True)

    # 不应抛异常
    assert util.get_ddddocr_server() is None
    assert util._ddddocr_init_failed is True
    # 失败后再次调用仍安全返回 None
    assert util.get_ddddocr_server() is None


def test_successful_init_is_cached(util, monkeypatch):
    """初始化成功 → 返回单例, 多次取用返回同一对象。"""
    monkeypatch.setattr(util, "ddddocr", types.SimpleNamespace(DdddOcr=_FakeDdddOcr))
    monkeypatch.setattr(util.config, "enable_ddddocr", True)

    first = util.get_ddddocr_server()
    second = util.get_ddddocr_server()
    assert first is not None
    assert first is second  # 命中缓存, 同一对象
