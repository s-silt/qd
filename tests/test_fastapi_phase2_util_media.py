#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase-2 smoke tests: FastAPI util OCR / media endpoints.

Endpoints under test
--------------------
GET  /util/dddd/ocr   – ddddocr character classification
POST /util/dddd/ocr
GET  /util/dddd/det   – ddddocr text detection (bounding boxes)
POST /util/dddd/det
GET  /util/dddd/slide – ddddocr slide/captcha matching
POST /util/dddd/slide
GET  /util/image      – image proxy (base64 or URL → raw bytes)
POST /util/image

Test cases
----------
1.  POST /util/dddd/ocr  with a mocked DDDDOCR_SERVER → 200 + Result
2.  POST /util/dddd/det  with a mocked DDDDOCR_SERVER → 200 + Result
3.  GET  /util/dddd/ocr  when ddddocr unavailable (DDDDOCR_SERVER=None) → 503
4.  POST /util/dddd/ocr  JSON body → 200 + Result
5.  POST /util/dddd/slide JSON body → 200 + Result
6.  GET  /util/dddd/det  when ddddocr unavailable → 503
7.  GET  /util/dddd/slide when ddddocr unavailable → 503
8.  GET  /util/image base64 → 200 image/png bytes
9.  POST /util/image JSON body → 200 image/png bytes
10. GET  /util/image no params → 415 / 400 (no image provided)
11. All media routes are registered in the app

ddddocr is NOT available in CI/sandbox – every test mocks DDDDOCR_SERVER
directly to avoid a real ddddocr import.

Skipped automatically when fastapi / httpx are not installed.
"""

import base64
import json
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Conditional skip guard
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed — skipping FastAPI Phase-2 util_media tests"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A minimal 1×1 white PNG in base64 – used as a test image payload.
_PNG_1x1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6"
    "kgAAAABJRU5ErkJggg=="
)
_PNG_1x1_BYTES = base64.b64decode(_PNG_1x1_BASE64)


def _make_app():
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


def _make_mock_ocr_server(classification_result="1234", detection_result=None, slide_result=None):
    """Return a MagicMock mimicking DdddOcrServer."""
    srv = MagicMock()
    srv.classification.return_value = classification_result
    srv.detection.return_value = detection_result or [[10, 20, 100, 50]]
    srv.slide_match.return_value = slide_result or {"target": [50, 50, 100, 100]}
    return srv


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestUtilMediaEndpoints(unittest.TestCase):
    """Smoke tests for /util/dddd/* and /util/image endpoints."""

    # ------------------------------------------------------------------
    # Case 1 – POST /util/dddd/ocr with mock server → 200 + Result
    # ------------------------------------------------------------------

    def test_ocr_post_form_with_mock_server(self):
        """Uploading a base64 image via form POST returns OCR result."""
        app = _make_app()
        mock_srv = _make_mock_ocr_server(classification_result="ABCD")

        with patch("web.fastapi.handlers.util_media.DDDDOCR_SERVER", mock_srv):
            with patch("web.fastapi.handlers.util_media._get_img", return_value=_PNG_1x1_BYTES):
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/util/dddd/ocr",
                    data={"img": _PNG_1x1_BASE64, "old": "False"},
                )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get("Result"), "ABCD")
        self.assertEqual(body.get("状态"), "OK")

    # ------------------------------------------------------------------
    # Case 2 – POST /util/dddd/det with mock server → 200 + Result
    # ------------------------------------------------------------------

    def test_det_post_json_with_mock_server(self):
        """Detection endpoint with JSON body returns bounding-box result."""
        app = _make_app()
        mock_srv = _make_mock_ocr_server(detection_result=[[5, 10, 80, 40]])

        with patch("web.fastapi.handlers.util_media.DDDDOCR_SERVER", mock_srv):
            with patch("web.fastapi.handlers.util_media._get_img", return_value=_PNG_1x1_BYTES):
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/util/dddd/det",
                    json={"img": _PNG_1x1_BASE64},
                )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get("Result"), [[5, 10, 80, 40]])
        self.assertEqual(body.get("状态"), "OK")

    # ------------------------------------------------------------------
    # Case 3 – GET /util/dddd/ocr when ddddocr unavailable → 503
    # ------------------------------------------------------------------

    def test_ocr_get_ddddocr_unavailable(self):
        """When DDDDOCR_SERVER is None the endpoint returns HTTP 503."""
        app = _make_app()

        with patch("web.fastapi.handlers.util_media.DDDDOCR_SERVER", None):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/util/dddd/ocr", params={"img": _PNG_1x1_BASE64})

        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertIn("ddddocr", body.get("状态", "").lower())

    # ------------------------------------------------------------------
    # Case 4 – POST /util/dddd/ocr JSON body → 200 + Result
    # ------------------------------------------------------------------

    def test_ocr_post_json_body(self):
        """OCR endpoint accepts application/json body."""
        app = _make_app()
        mock_srv = _make_mock_ocr_server(classification_result="9876")

        with patch("web.fastapi.handlers.util_media.DDDDOCR_SERVER", mock_srv):
            with patch("web.fastapi.handlers.util_media._get_img", return_value=_PNG_1x1_BYTES):
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/util/dddd/ocr",
                    json={"img": _PNG_1x1_BASE64, "old": "False", "extra_onnx_name": ""},
                )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get("Result"), "9876")
        self.assertEqual(body.get("状态"), "OK")

    # ------------------------------------------------------------------
    # Case 5 – POST /util/dddd/slide JSON body → 200 + Result
    # ------------------------------------------------------------------

    def test_slide_post_json_with_mock_server(self):
        """Slide-match endpoint with JSON body returns coordinate result."""
        app = _make_app()
        slide_result = {"target": [60, 60, 120, 120]}
        mock_srv = _make_mock_ocr_server(slide_result=slide_result)

        with patch("web.fastapi.handlers.util_media.DDDDOCR_SERVER", mock_srv):
            with patch("web.fastapi.handlers.util_media._get_img", return_value=_PNG_1x1_BYTES):
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/util/dddd/slide",
                    json={
                        "imgtarget": _PNG_1x1_BASE64,
                        "imgbg": _PNG_1x1_BASE64,
                        "comparison": "False",
                        "simple_target": "False",
                    },
                )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get("Result"), slide_result)
        self.assertEqual(body.get("状态"), "OK")

    # ------------------------------------------------------------------
    # Case 6 – GET /util/dddd/det when ddddocr unavailable → 503
    # ------------------------------------------------------------------

    def test_det_get_ddddocr_unavailable(self):
        """Detection GET returns 503 when ddddocr is not available."""
        app = _make_app()

        with patch("web.fastapi.handlers.util_media.DDDDOCR_SERVER", None):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/util/dddd/det", params={"img": _PNG_1x1_BASE64})

        self.assertEqual(resp.status_code, 503)

    # ------------------------------------------------------------------
    # Case 7 – GET /util/dddd/slide when ddddocr unavailable → 503
    # ------------------------------------------------------------------

    def test_slide_get_ddddocr_unavailable(self):
        """Slide GET returns 503 when ddddocr is not available."""
        app = _make_app()

        with patch("web.fastapi.handlers.util_media.DDDDOCR_SERVER", None):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/util/dddd/slide",
                params={"imgtarget": _PNG_1x1_BASE64, "imgbg": _PNG_1x1_BASE64},
            )

        self.assertEqual(resp.status_code, 503)

    # ------------------------------------------------------------------
    # Case 8 – GET /util/image with base64 data → 200 image/png
    # ------------------------------------------------------------------

    def test_image_get_base64(self):
        """GET /util/image with a base64-encoded PNG returns image bytes."""
        app = _make_app()

        with patch("web.fastapi.handlers.util_media._get_img", return_value=_PNG_1x1_BYTES):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/util/image", params={"img": _PNG_1x1_BASE64})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("image/png", resp.headers.get("content-type", ""))
        self.assertEqual(resp.content, _PNG_1x1_BYTES)

    # ------------------------------------------------------------------
    # Case 9 – POST /util/image JSON body → 200 image/png
    # ------------------------------------------------------------------

    def test_image_post_json(self):
        """POST /util/image with JSON body returns raw image bytes."""
        app = _make_app()

        with patch("web.fastapi.handlers.util_media._get_img", return_value=_PNG_1x1_BYTES):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/util/image", json={"img": _PNG_1x1_BASE64})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("image/png", resp.headers.get("content-type", ""))
        self.assertEqual(resp.content, _PNG_1x1_BYTES)

    # ------------------------------------------------------------------
    # Case 10 – GET /util/image with no params → 415
    # ------------------------------------------------------------------

    def test_image_get_no_params(self):
        """GET /util/image with no img/imgurl returns a client error."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/util/image")
        # 415 (HTTPException from _get_img) or 400 (re-raised)
        self.assertIn(resp.status_code, (400, 415))

    # ------------------------------------------------------------------
    # Case 11 – Route registration sanity check
    # ------------------------------------------------------------------

    def test_all_media_routes_registered(self):
        """All media routes are registered in the FastAPI app."""
        app = _make_app()
        paths = {r.path for r in app.routes}
        expected = {
            "/util/dddd/ocr",
            "/util/dddd/det",
            "/util/dddd/slide",
            "/util/image",
        }
        for path in expected:
            self.assertIn(path, paths, f"Route {path!r} not registered")


if __name__ == "__main__":
    unittest.main()
