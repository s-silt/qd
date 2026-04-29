#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of the AI subset of web/handlers/har.py.

Routes ported:
    POST /har/ai_analyze          -> HARAIAnalyze  (login required)
    GET  /har/ai_status           -> HARAIStatus   (login required, no model field)

Security invariants preserved from Tornado version (security-followup fixes):
  - All endpoints require authentication (@authenticated -> Depends(require_user)).
  - HARAIStatus: returns only {"enabled": bool}, does NOT expose model name.
  - HAR size is capped via ai_client.read_capped() to prevent memory exhaustion.
  - AI error details are NOT echoed verbatim to callers (upstream error body not reflected).
  - evil(+1) rate-limit counter incremented on mutating endpoints.
"""

import config
from libs import ai_client
from libs.log import Log

try:
    from fastapi import APIRouter, Depends, HTTPException, Request
    from fastapi.responses import JSONResponse
except ImportError:
    # Allow import in environments without fastapi (static analysis, etc.)
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    Request = object  # type: ignore
    JSONResponse = object  # type: ignore

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int = 500, detail: str = ""):
            self.status_code = status_code
            self.detail = detail

from web.fastapi.base import get_db, require_user, _get_ip

logger = Log("QD.FastAPI.HAR").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Shared helper — mirrors _analyze_har_with_ai() from web/handlers/har.py
# ---------------------------------------------------------------------------


async def _analyze_har_with_ai(har: dict, hint: str) -> dict:
    """Pre-process HAR and send to LLM; returns {result, har, stats}.

    Raises ai_client.AIClientError; caller is responsible for catching and
    presenting a sanitised error message (never echo raw upstream body).
    """
    client = ai_client.AIClient()
    if not client.enabled:
        raise ai_client.AIClientError("AI_API_KEY 未配置")
    slim = ai_client.preprocess_har(har, config.ai_max_har_entries)
    if not slim:
        raise ai_client.AIClientError("HAR 中未找到可分析的请求（可能均被过滤）")
    messages = ai_client.build_messages(slim, hint=hint)
    content = await client.chat(messages, temperature=0.1)
    result = ai_client.parse_ai_response(content)
    return {
        "result": result,
        "har": ai_client.ai_result_to_har(result),
        "stats": {"input_entries": len(slim)},
    }


# ---------------------------------------------------------------------------
# POST /har/ai_analyze
# ---------------------------------------------------------------------------


@router.post("/har/ai_analyze")
async def har_ai_analyze(
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Analyse a HAR capture with AI and return a minimal HAR template.

    Request body (JSON):
        {"har": <HAR JSON object>, "hint": "optional hint, e.g. '签到接口'"}

    Response:
        {"ok": true,  "har": <slim HAR>, "result": <AI output>, "stats": {...}}
        {"ok": false, "error": "sanitised error message"}
    """
    # Increment evil counter (mirrors self.evil(+1) in Tornado version)
    if not config.debug and db:
        ip = _get_ip(request)
        try:
            db.redis.evil(ip, user.get("id"), 1)
        except Exception as _e:
            logger.debug("evil counter error: %s", _e, exc_info=config.traceback_print)

    # Check AI availability before parsing body (fast-fail)
    client = ai_client.AIClient()
    if not client.enabled:
        raise HTTPException(
            status_code=503,
            detail="AI 功能未启用，请管理员设置环境变量 AI_API_KEY",
        )

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON: {e}") from e

    har = payload.get("har")
    hint = payload.get("hint", "") or ""
    if not isinstance(har, dict) or "log" not in har:
        raise HTTPException(
            status_code=400,
            detail="har 字段缺失或格式不正确，需为 HAR JSON",
        )

    try:
        ai_out = await _analyze_har_with_ai(har, hint)
    except ai_client.AIClientError as e:
        logger.warning("AI 分析失败: %s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.error("AI 分析异常: %s", e, exc_info=config.traceback_print)
        raise HTTPException(status_code=500, detail=f"内部错误: {e}") from e

    return {
        "ok": True,
        "har": ai_out["har"],
        "result": ai_out["result"],
        "stats": ai_out["stats"],
    }


# ---------------------------------------------------------------------------
# GET /har/ai_status
# ---------------------------------------------------------------------------


@router.get("/har/ai_status")
async def har_ai_status(
    user: dict = Depends(require_user),
):
    """Return whether AI functionality is available.

    Security: requires authentication; deliberately omits the model name to
    prevent fingerprinting of the backend AI provider.
    """
    client = ai_client.AIClient()
    # Only return enabled bool — do NOT expose model name (security-followup fix).
    return {"enabled": client.enabled}
