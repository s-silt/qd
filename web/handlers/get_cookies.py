#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Landing page + zip download for the bundled QD Cookies Helper extension.

Routes:
  GET /get-cookies/         install instructions, bookmarklet, status check
  GET /get-cookies/download streams a freshly-built zip of web/extension/get-cookies/
"""

import io
import os
import zipfile

from tornado.web import addslash

from web.handlers.base import BaseHandler

EXTENSION_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "extension", "get-cookies")
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


class GetCookiesHandler(BaseHandler):
    @addslash
    async def get(self):
        await self.render("get_cookies.html")


class GetCookiesDownloadHandler(BaseHandler):
    async def get(self):
        if not os.path.isdir(EXTENSION_DIR):
            self.set_status(404)
            self.write("extension source not found")
            return
        payload = _build_zip()
        self.set_header("Content-Type", "application/zip")
        self.set_header(
            "Content-Disposition",
            'attachment; filename="qd-get-cookies.zip"',
        )
        self.set_header("Content-Length", str(len(payload)))
        self.write(payload)


handlers = [
    (r"/get-cookies/?", GetCookiesHandler),
    (r"/get-cookies/download/?", GetCookiesDownloadHandler),
]
