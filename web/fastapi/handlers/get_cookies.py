#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/get_cookies.py.

Routes:
  GET /get-cookies/         install instructions, bookmarklet, status check
  GET /get-cookies/download streams a freshly-built zip of web/extension/get-cookies/
"""

import io
import os
import zipfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from web.fastapi.templates import render_template

router = APIRouter()

EXTENSION_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir, "extension", "get-cookies"
    )
)


def _build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(EXTENSION_DIR):
            for name in files:
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, EXTENSION_DIR)
                zf.write(abs_path, rel_path)
    return buf.getvalue()


@router.get("/get-cookies")
@router.get("/get-cookies/")
async def get_cookies_page(request: Request):
    return render_template(request, "get_cookies.html")


@router.get("/get-cookies/download")
@router.get("/get-cookies/download/")
async def get_cookies_download():
    if not os.path.isdir(EXTENSION_DIR):
        raise HTTPException(status_code=404, detail="extension source not found")
    payload = _build_zip()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="qd-get-cookies.zip"',
        },
    )
