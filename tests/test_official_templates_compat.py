#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""官方签到模板仓库 (qd-today/templates) 与 QD 的格式兼容性单测。

官方仓库通过「公共模板订阅」拉取 tpls_history.json:
  - 每条 har[].content 为 base64(QD 模板 JSON 数组)，或 content=="" 时按 filename 拉 .har
  - .har 文件本身通常已是 QD 步骤数组 (request/rule)，不是浏览器 HAR 的 log.entries
  - 前端 HARDATA→Base64.decode→utils.tpl2har；保存时 har2tpl 再变回数组给 Fetcher

本测试不依赖网络；优先读 Desktop 旁的浅克隆目录，找不到则 skip。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, List, Optional

import pytest

from libs.fetcher import Fetcher

# 候选路径: 与 qd-refactor 同级的浅克隆 / 环境变量 / 仓库内可选子目录
_CANDIDATES = [
    os.environ.get("QD_OFFICIAL_TEMPLATES_DIR"),
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "qd-today-templates")
    ),
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "qd-today-templates")
    ),
    r"C:\Users\sxl\Desktop\qd-today-templates",
]


def _find_official_dir() -> Optional[str]:
    for path in _CANDIDATES:
        if not path:
            continue
        hist = os.path.join(path, "tpls_history.json")
        if os.path.isfile(hist):
            return path
    return None


OFFICIAL_DIR = _find_official_dir()
skip_no_official = pytest.mark.skipif(
    OFFICIAL_DIR is None,
    reason="official qd-today/templates clone not found (set QD_OFFICIAL_TEMPLATES_DIR)",
)


def _load_history() -> Dict[str, Any]:
    assert OFFICIAL_DIR
    with open(
        os.path.join(OFFICIAL_DIR, "tpls_history.json"), encoding="utf-8"
    ) as f:
        return json.load(f)


def _decode_entry(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """模拟 subscribe 落库后的 content 解码 / filename 回源。"""
    content = meta.get("content") or ""
    if content:
        raw = base64.b64decode(content)
        tpl = json.loads(raw)
    else:
        filename = meta.get("filename") or ""
        assert OFFICIAL_DIR and filename
        path = os.path.join(OFFICIAL_DIR, filename)
        with open(path, encoding="utf-8") as f:
            tpl = json.load(f)
    assert isinstance(tpl, list) and tpl, "official tpl must be non-empty QD array"
    return tpl


def _har2tpl(har: Dict[str, Any]) -> List[Dict[str, Any]]:
    """对齐 web/static/har/entry_list.js 的 har2tpl (仅 checked 条目)。"""
    out: List[Dict[str, Any]] = []
    for entry in har["log"]["entries"]:
        if not entry.get("checked"):
            continue
        req = entry["request"]
        post = req.get("postData") or {}
        out.append(
            {
                "comment": entry.get("comment"),
                "request": {
                    "method": req["method"],
                    "url": req["url"],
                    "headers": [
                        {"name": h["name"], "value": h["value"]}
                        for h in (req.get("headers") or [])
                        if h.get("checked")
                    ],
                    "cookies": [
                        {"name": c["name"], "value": c["value"]}
                        for c in (req.get("cookies") or [])
                        if c.get("checked")
                    ],
                    "data": post.get("text"),
                    "mimeType": post.get("mimeType"),
                },
                "rule": {
                    "success_asserts": entry.get("success_asserts"),
                    "failed_asserts": entry.get("failed_asserts"),
                    "extract_variables": entry.get("extract_variables"),
                },
            }
        )
    return out


@skip_no_official
def test_tpls_history_schema():
    data = _load_history()
    assert "version" in data and "har" in data
    assert isinstance(data["har"], dict) and data["har"]
    # 抽样检查索引字段 (subscribe.py 依赖 name/version/filename/content)
    sample = next(iter(data["har"].values()))
    for key in ("name", "filename", "version", "date"):
        assert key in sample
    assert "content" in sample  # 可为 ""


@skip_no_official
def test_all_official_entries_tpl2har_and_har2tpl_roundtrip():
    data = _load_history()
    ok = 0
    for name, meta in data["har"].items():
        tpl = _decode_entry(meta)
        for i, step in enumerate(tpl):
            assert "request" in step and "rule" in step, f"{name} step{i}"
            assert "method" in step["request"] and "url" in step["request"]
        har = Fetcher.tpl2har(tpl)
        assert len(har["log"]["entries"]) == len(tpl)
        back = _har2tpl(har)
        assert len(back) == len(tpl)
        for a, b in zip(tpl, back):
            assert a["request"]["method"] == b["request"]["method"]
            assert a["request"]["url"] == b["request"]["url"]
            # rules round-trip; tpl2har fills missing keys as []
            def _norm(v):
                return [] if v is None else v
            ar, br = a.get("rule") or {}, b["rule"]
            assert _norm(ar.get("success_asserts")) == _norm(br.get("success_asserts"))
            assert _norm(ar.get("failed_asserts")) == _norm(br.get("failed_asserts"))
            assert _norm(ar.get("extract_variables")) == _norm(br.get("extract_variables"))
        ok += 1
    assert ok == len(data["har"])


@skip_no_official
def test_sample_official_har_files_are_qd_arrays_not_browser_har():
    """官方 .har 多为 QD 数组；上传路径依赖 if (data.log) else utils.tpl2har(data)。"""
    assert OFFICIAL_DIR
    samples = ["V2EX.har", "glados.har", "hostloc主机论坛.har"]
    found = 0
    for name in samples:
        path = os.path.join(OFFICIAL_DIR, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        assert isinstance(obj, list), f"{name} should be QD array"
        assert "log" not in (obj[0] if obj else {})
        found += 1
    assert found >= 1


@skip_no_official
def test_fetcher_build_request_on_official_sample():
    data = _load_history()
    # 取一条含 content 的条目
    meta = None
    for m in data["har"].values():
        if m.get("content"):
            meta = m
            break
    assert meta
    tpl = _decode_entry(meta)
    fetcher = Fetcher()
    env: Dict[str, Any] = {"session": [], "variables": {}}
    for step in tpl:
        # 占位变量避免渲染失败；缺省过滤器仍可能需要
        req, rule, env = fetcher.build_request(
            {"request": step["request"], "rule": step.get("rule") or {}, "env": env}
        )
        assert req is not None
        assert hasattr(req, "url") and req.url
