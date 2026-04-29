#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of OCR / image endpoints from web/handlers/util.py.

Endpoints ported
----------------
GET/POST /util/dddd/ocr    – ddddocr character recognition
GET/POST /util/dddd/det    – ddddocr text detection (bounding boxes)
GET/POST /util/dddd/slide  – ddddocr slide/captcha matching

Scope: only handlers that depend on ddddocr / Pillow / cv2.
Simple utility endpoints (delay, timestamp, unicode, AES, base64) are
ported by a separate agent.

ddddocr unavailability
----------------------
If ddddocr is not installed, ``DDDDOCR_SERVER`` is ``None`` and every
endpoint returns HTTP 503 with a JSON body describing the unavailability.
"""

import base64
import json
import os
from typing import Optional

import aiohttp
import config
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from libs.log import Log

logger = Log("QD.FastAPI.UtilMedia").getlogger()

# ---------------------------------------------------------------------------
# Optional ddddocr import (mirrors web/handlers/util.py)
# ---------------------------------------------------------------------------

try:
    import ddddocr  # type: ignore
except ImportError as _e:
    if config.display_import_warning:
        logger.warning(
            'Import DdddOCR module failed: "%s". '
            "This warning is only for prompting; it will not affect the QD framework.",
            _e,
        )
    ddddocr = None

# ---------------------------------------------------------------------------
# DdddOcrServer wrapper (mirrors web/handlers/util.py:DdddOcrServer)
# ---------------------------------------------------------------------------


class DdddOcrServer:
    def __init__(self):
        if ddddocr is not None and hasattr(ddddocr, "DdddOcr"):
            self.oldocr = ddddocr.DdddOcr(old=True, show_ad=False)
            self.ocr = ddddocr.DdddOcr(show_ad=False)
            self.det = ddddocr.DdddOcr(det=True, show_ad=False)
            self.slide = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
            self.extra: dict = {}
            if (
                len(config.extra_onnx_name) == len(config.extra_charsets_name)
                and config.extra_onnx_name[0]
                and config.extra_charsets_name[0]
            ):
                for onnx_name in config.extra_onnx_name:
                    self.extra[onnx_name] = ddddocr.DdddOcr(
                        show_ad=False,
                        import_onnx_path=os.path.join(
                            os.path.abspath(
                                os.path.dirname(
                                    os.path.dirname(
                                        os.path.dirname(os.path.dirname(__file__))
                                    )
                                )
                            ),
                            "config",
                            f"{onnx_name}.onnx",
                        ),
                        charsets_path=os.path.join(
                            os.path.abspath(
                                os.path.dirname(
                                    os.path.dirname(
                                        os.path.dirname(os.path.dirname(__file__))
                                    )
                                )
                            ),
                            "config",
                            f"{onnx_name}.json",
                        ),
                    )
                    logger.info("Loaded custom Onnx model: %s.onnx", onnx_name)

    def classification(self, img: bytes, old: bool = False, extra_onnx_name: str = "") -> str:
        if extra_onnx_name:
            return self.extra[extra_onnx_name].classification(img)
        if old:
            return self.oldocr.classification(img)
        return self.ocr.classification(img)

    def detection(self, img: bytes):
        return self.det.detection(img)

    def slide_match(
        self,
        imgtarget: bytes,
        imgbg: bytes,
        comparison: bool = False,
        simple_target: bool = False,
    ):
        if comparison:
            return self.slide.slide_comparison(imgtarget, imgbg)
        if not simple_target:
            try:
                return self.slide.slide_match(imgtarget, imgbg)
            except Exception as e:
                logger.debug("slide_match error: %s", e, exc_info=config.traceback_print)
        return self.slide.slide_match(imgtarget, imgbg, simple_target=True)


# Singleton – None when ddddocr unavailable
if ddddocr:
    try:
        DDDDOCR_SERVER: Optional[DdddOcrServer] = DdddOcrServer()
    except Exception as _init_e:
        logger.warning("DdddOcrServer init failed: %s", _init_e)
        DDDDOCR_SERVER = None
else:
    DDDDOCR_SERVER = None

# ---------------------------------------------------------------------------
# Image fetching helpers (mirrors web/handlers/util.py)
# ---------------------------------------------------------------------------


async def _get_img_from_url(imgurl: str) -> bytes:
    async with aiohttp.ClientSession(conn_timeout=config.connect_timeout) as session:
        async with session.get(
            imgurl, verify_ssl=False, timeout=config.request_timeout
        ) as res:
            content = await res.read()
            base64_data = base64.b64encode(content).decode()
            return base64.b64decode(base64_data)


async def _get_img(img: str = "", imgurl: str = "") -> bytes:
    """Resolve a base64-encoded image or a URL to raw bytes."""
    if img:
        if img.startswith("http"):
            try:
                return await _get_img_from_url(img)
            except Exception as e:
                logger.debug("get_img_from_url error: %s", e, exc_info=config.traceback_print)
                return base64.b64decode(img)
        return base64.b64decode(img)
    elif imgurl:
        return await _get_img_from_url(imgurl)
    else:
        raise HTTPException(status_code=415, detail="No image provided (img or imgurl required)")


def _strtobool(val: str) -> bool:
    """Mirror config.strtobool but local to avoid import coupling."""
    from config import strtobool
    return bool(strtobool(val))


def _ddddocr_unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"Result": None, "状态": "ddddocr not available"},
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


# ── /util/dddd/ocr ──────────────────────────────────────────────────────────

@router.get("/util/dddd/ocr")
async def dddd_ocr_get(
    img: str = Query(default=""),
    imgurl: str = Query(default=""),
    old: str = Query(default="False"),
    extra_onnx_name: str = Query(default=""),
):
    """GET: ddddocr character classification (base64 img or URL)."""
    rtv: dict = {}
    try:
        if not DDDDOCR_SERVER:
            return _ddddocr_unavailable()
        img_bytes = await _get_img(img, imgurl)
        rtv["Result"] = DDDDOCR_SERVER.classification(
            img_bytes, old=_strtobool(old), extra_onnx_name=extra_onnx_name
        )
        rtv["状态"] = "OK"
    except HTTPException:
        raise
    except Exception as e:
        rtv["状态"] = str(e)

    return JSONResponse(
        content=json.loads(json.dumps(rtv, ensure_ascii=False, indent=4))
    )


@router.post("/util/dddd/ocr")
async def dddd_ocr_post(request: Request):
    """POST: ddddocr character classification (JSON body or form data)."""
    rtv: dict = {}
    try:
        if not DDDDOCR_SERVER:
            return _ddddocr_unavailable()

        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            body_dict = await request.json()
            img = body_dict.get("img", "")
            imgurl = body_dict.get("imgurl", "")
            old = str(body_dict.get("old", "False"))
            extra_onnx_name = body_dict.get("extra_onnx_name", "")
        else:
            form = await request.form()
            img = form.get("img", "")
            imgurl = form.get("imgurl", "")
            old = form.get("old", "False")
            extra_onnx_name = form.get("extra_onnx_name", "")

        img_bytes = await _get_img(img, imgurl)
        rtv["Result"] = DDDDOCR_SERVER.classification(
            img_bytes, old=_strtobool(old), extra_onnx_name=extra_onnx_name
        )
        rtv["状态"] = "OK"
    except HTTPException:
        raise
    except Exception as e:
        rtv["状态"] = str(e)

    return JSONResponse(
        content=json.loads(json.dumps(rtv, ensure_ascii=False, indent=4))
    )


# ── /util/dddd/det ──────────────────────────────────────────────────────────

@router.get("/util/dddd/det")
async def dddd_det_get(
    img: str = Query(default=""),
    imgurl: str = Query(default=""),
):
    """GET: ddddocr text detection (bounding boxes)."""
    rtv: dict = {}
    try:
        if not DDDDOCR_SERVER:
            return _ddddocr_unavailable()
        img_bytes = await _get_img(img, imgurl)
        rtv["Result"] = DDDDOCR_SERVER.detection(img_bytes)
        rtv["状态"] = "OK"
    except HTTPException:
        raise
    except Exception as e:
        rtv["状态"] = str(e)

    return JSONResponse(
        content=json.loads(json.dumps(rtv, ensure_ascii=False))
    )


@router.post("/util/dddd/det")
async def dddd_det_post(request: Request):
    """POST: ddddocr text detection (JSON body or form data)."""
    rtv: dict = {}
    try:
        if not DDDDOCR_SERVER:
            return _ddddocr_unavailable()

        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            body_dict = await request.json()
            img = body_dict.get("img", "")
            imgurl = body_dict.get("imgurl", "")
        else:
            form = await request.form()
            img = form.get("img", "")
            imgurl = form.get("imgurl", "")

        img_bytes = await _get_img(img, imgurl)
        rtv["Result"] = DDDDOCR_SERVER.detection(img_bytes)
        rtv["状态"] = "OK"
    except HTTPException:
        raise
    except Exception as e:
        rtv["状态"] = str(e)

    return JSONResponse(
        content=json.loads(json.dumps(rtv, ensure_ascii=False))
    )


# ── /util/dddd/slide ────────────────────────────────────────────────────────

@router.get("/util/dddd/slide")
async def dddd_slide_get(
    imgtarget: str = Query(default=""),
    imgbg: str = Query(default=""),
    simple_target: str = Query(default="False"),
    comparison: str = Query(default="False"),
):
    """GET: ddddocr slide/captcha matching."""
    rtv: dict = {}
    try:
        if not DDDDOCR_SERVER:
            return _ddddocr_unavailable()
        target_bytes = await _get_img(imgtarget, "")
        bg_bytes = await _get_img(imgbg, "")
        rtv["Result"] = DDDDOCR_SERVER.slide_match(
            target_bytes,
            bg_bytes,
            comparison=_strtobool(comparison),
            simple_target=_strtobool(simple_target),
        )
        rtv["状态"] = "OK"
    except HTTPException:
        raise
    except Exception as e:
        rtv["状态"] = str(e)

    return JSONResponse(
        content=json.loads(json.dumps(rtv, ensure_ascii=False))
    )


@router.post("/util/dddd/slide")
async def dddd_slide_post(request: Request):
    """POST: ddddocr slide/captcha matching (JSON body or form data)."""
    rtv: dict = {}
    try:
        if not DDDDOCR_SERVER:
            return _ddddocr_unavailable()

        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            body_dict = await request.json()
            imgtarget = body_dict.get("imgtarget", "")
            imgbg = body_dict.get("imgbg", "")
            simple_target = str(body_dict.get("simple_target", "False"))
            comparison = str(body_dict.get("comparison", "False"))
        else:
            form = await request.form()
            imgtarget = form.get("imgtarget", "")
            imgbg = form.get("imgbg", "")
            simple_target = form.get("simple_target", "False")
            comparison = form.get("comparison", "False")

        target_bytes = await _get_img(imgtarget, "")
        bg_bytes = await _get_img(imgbg, "")
        rtv["Result"] = DDDDOCR_SERVER.slide_match(
            target_bytes,
            bg_bytes,
            comparison=_strtobool(comparison),
            simple_target=_strtobool(simple_target),
        )
        rtv["状态"] = "OK"
    except HTTPException:
        raise
    except Exception as e:
        rtv["状态"] = str(e)

    return JSONResponse(
        content=json.loads(json.dumps(rtv, ensure_ascii=False))
    )
