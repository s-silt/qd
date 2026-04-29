#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/about.py.

Original Tornado handler (22 lines):
    class AboutHandler(BaseHandler):
        @addslash
        async def get(self):
            await self.render('about.html')

This version is functionally equivalent:
  - GET /about  (also accepts /about/ — trailing slash handled by FastAPI redirect)
  - Renders about.html via Jinja2 with the standard namespace
  - No login required (same as original)
"""

from fastapi import APIRouter, Request

from web.fastapi.templates import render_template

router = APIRouter()


@router.get("/about")
async def about(request: Request):
    """Render the QD about / API reference page."""
    return render_template(request, "about.html")
