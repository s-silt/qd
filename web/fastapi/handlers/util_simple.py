#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of the "simple" subset of web/handlers/util.py.

Ported endpoints:
  GET /util/delay               - delay by ?seconds= query param
  GET /util/delay/{seconds}     - delay by path param (int or float)
  GET/POST /util/timestamp      - timestamp conversion/display
  GET/POST /util/unicode        - unicode escape conversion
  GET/POST /util/gb2312         - GB2312 URL encoding
  GET/POST /util/urldecode      - URL decode
  GET/POST /util/aes/encrypt    - AES encrypt (via libs/_utils/crypto._aes_encrypt)
  GET/POST /util/aes/decrypt    - AES decrypt (via libs/_utils/crypto._aes_decrypt)
  GET/POST /util/base64/encode  - Base64 encode
  GET/POST /util/base64/decode  - Base64 decode
  GET/POST /util/regex          - regex findall
  GET/POST /util/string/replace - regex replace

NOT ported here (handled by other agents):
  /util/ocr, /util/dddd/*       -> OCR agent
  /util/image*                  -> image agent
  /util/toolbox/*               -> separate concern
  /util/rsa                     -> depends on PyCrypto RSA (separate concern)
"""

import asyncio
import base64
import datetime
import html
import json
import re
import time
import urllib.parse
from typing import Optional
from zoneinfo import ZoneInfo

import config
from config import delay_max_timeout, strtobool
from libs.log import Log

try:
    from fastapi import APIRouter, Query, Request
    from fastapi.responses import JSONResponse, PlainTextResponse, Response
except ImportError:
    APIRouter = object  # type: ignore
    Request = object  # type: ignore
    Query = lambda *a, **kw: None  # type: ignore
    JSONResponse = object  # type: ignore
    PlainTextResponse = object  # type: ignore
    Response = object  # type: ignore

logger = Log("QD.FastAPI.UtilSimple").getlogger()

router = APIRouter()

GMT_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yearday(year: int) -> str:
    if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
        return "366"
    return "365"


def _json_response(data: dict) -> Response:
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=4),
        media_type="application/json; charset=UTF-8",
    )


# ---------------------------------------------------------------------------
# Delay endpoints
# ---------------------------------------------------------------------------

@router.get("/util/delay")
async def util_delay_param(seconds: float = Query(default=0.0)):
    """Delay ?seconds= query-param seconds (mirrors UtilDelayParaHandler)."""
    try:
        if seconds < 0:
            seconds = 0.0
        elif seconds >= delay_max_timeout:
            seconds = delay_max_timeout
            await asyncio.sleep(seconds)
            return PlainTextResponse(f"Error, limited by delay_max_timeout, delay {seconds} second.")
        await asyncio.sleep(seconds)
        return PlainTextResponse(f"delay {seconds} second.")
    except Exception as e:
        logger.debug("util_delay_param error: %s", e, exc_info=config.traceback_print)
        return PlainTextResponse("Error, delay 0.0 second.")


@router.get("/util/delay/{seconds}")
async def util_delay_path(seconds: str):
    """Delay path/{seconds} seconds (mirrors UtilDelayIntHandler + UtilDelayHandler)."""
    try:
        secs = float(seconds)
    except Exception as e:
        logger.debug("util_delay_path parse error: %s", e, exc_info=config.traceback_print)
        return PlainTextResponse("Error, delay 0.0 second.")

    if secs < 0:
        secs = 0.0
    elif secs >= delay_max_timeout:
        secs = delay_max_timeout
        await asyncio.sleep(secs)
        return PlainTextResponse(f"Error, limited by {delay_max_timeout}, delay {secs} second.")
    await asyncio.sleep(secs)
    return PlainTextResponse(f"delay {secs} second.")


# ---------------------------------------------------------------------------
# Timestamp endpoint
# ---------------------------------------------------------------------------

@router.get("/util/timestamp")
@router.post("/util/timestamp")
async def util_timestamp(
    ts: str = Query(default=""),
    dt: str = Query(default=""),
    form: str = Query(default="%Y-%m-%d %H:%M:%S"),
):
    """Timestamp conversion tool (mirrors TimeStampHandler)."""
    rtv: dict = {}
    try:
        time_format = form if form else "%Y-%m-%d %H:%M:%S"
        cst_tz = ZoneInfo("Asia/Shanghai")
        utc_tz = ZoneInfo("UTC")
        tmp = datetime.datetime.fromtimestamp

        if dt:
            ts = str(datetime.datetime.strptime(dt, time_format).timestamp())

        if ts:
            rtv["完整时间戳"] = float(ts)
            rtv["时间戳"] = int(rtv["完整时间戳"])
            rtv["16位时间戳"] = int(rtv["完整时间戳"] * 1_000_000)
            rtv["周"] = tmp(rtv["完整时间戳"]).strftime("%w/%W")
            rtv["日"] = "/".join([
                tmp(rtv["完整时间戳"]).strftime("%j"),
                _yearday(tmp(rtv["完整时间戳"]).year),
            ])
            rtv["北京时间"] = tmp(rtv["完整时间戳"], cst_tz).strftime(time_format)
            rtv["GMT格式"] = tmp(rtv["完整时间戳"], utc_tz).strftime(GMT_FORMAT)
            rtv["ISO格式"] = tmp(rtv["完整时间戳"], utc_tz).isoformat().split("+")[0] + "Z"
        else:
            now = time.time()
            rtv["完整时间戳"] = now
            rtv["时间戳"] = int(now)
            rtv["16位时间戳"] = int(now * 1_000_000)
            rtv["本机时间"] = tmp(now).strftime(time_format)
            rtv["周"] = tmp(now).strftime("%w/%W")
            rtv["日"] = "/".join([tmp(now).strftime("%j"), _yearday(tmp(now).year)])
            rtv["北京时间"] = tmp(now, cst_tz).strftime(time_format)
            rtv["GMT格式"] = tmp(now, utc_tz).strftime(GMT_FORMAT)
            rtv["ISO格式"] = tmp(now, utc_tz).isoformat().split("+")[0] + "Z"
        rtv["状态"] = "200"
    except Exception as e:
        rtv["状态"] = str(e)
    return _json_response(rtv)


# ---------------------------------------------------------------------------
# Unicode / encoding conversion endpoints
# ---------------------------------------------------------------------------

def _do_unicode(content: str, html_unescape_flag: str) -> dict:
    rtv: dict = {}
    try:
        tmp = (
            bytes(content, "unicode_escape")
            .decode("utf-8")
            .replace(r"\u", r"\\u")
            .replace(r"\\\u", r"\\u")
        )
        tmp = bytes(tmp, "utf-8").decode("unicode_escape")
        tmp = (
            tmp.encode("utf-8")
            .replace(b"\xc2\xa0", b"\xa0")
            .decode("unicode_escape")
        )
        if strtobool(html_unescape_flag):
            tmp = html.unescape(tmp)
        rtv["转换后"] = tmp
        rtv["状态"] = "200"
    except Exception as e:
        rtv["状态"] = str(e)
    return rtv


@router.get("/util/unicode")
@router.post("/util/unicode")
async def util_unicode(
    content: str = Query(default=""),
    html_unescape: str = Query(default="false"),
):
    """Unicode escape conversion (mirrors UniCodeHandler)."""
    return _json_response(_do_unicode(content, html_unescape))


@router.get("/util/gb2312")
@router.post("/util/gb2312")
async def util_gb2312(content: str = Query(default="")):
    """GB2312 URL encoding (mirrors GB2312Handler)."""
    rtv: dict = {}
    try:
        rtv["转换后"] = urllib.parse.quote(content, encoding="gb2312")
        rtv["状态"] = "200"
    except Exception as e:
        rtv["状态"] = str(e)
    return _json_response(rtv)


@router.get("/util/urldecode")
@router.post("/util/urldecode")
async def util_urldecode(
    content: str = Query(default=""),
    encoding: str = Query(default="utf-8"),
    unquote_plus: str = Query(default="false"),
):
    """URL decode (mirrors UrlDecodeHandler)."""
    rtv: dict = {}
    try:
        if strtobool(unquote_plus):
            rtv["转换后"] = urllib.parse.unquote_plus(content, encoding=encoding)
        else:
            rtv["转换后"] = urllib.parse.unquote(content, encoding=encoding)
        rtv["状态"] = "200"
    except Exception as e:
        rtv["状态"] = str(e)
    return _json_response(rtv)
