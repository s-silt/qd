#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Phase-2 smoke tests: FastAPI HAR AI analyze + AutoCapture endpoints.

Covers (minimum 4 required cases):
  1. GET /har/ai_status       anonymous      → 401 (auth gate)
  2. GET /har/ai_status       authenticated  → 200, no 'model' field
  3. GET /har/auto_capture_status authenticated → 200, no 'sidecar_url' field
  4. POST /har/ai_analyze     AI not configured → 503

Additional cases:
  5. POST /har/ai_analyze     missing har field → 400
  6. POST /har/ai_analyze     anonymous → 401
  7. POST /har/ai_analyze     valid HAR + mocked AI → 200
  8. GET /har/auto_capture_status anonymous → 401
  9. GET /har/auto_capture_status sidecar configured → enabled=True
  10. POST /har/auto_capture  anonymous → 401
  11. POST /har/auto_capture  sidecar not configured → 503
  12. POST /har/auto_capture  missing url field → 400
  13. All four routes registered in app

Skipped automatically when fastapi / httpx / umsgpack are not installed.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Conditional skip guard — pytest.importorskip handles missing dependencies
# ---------------------------------------------------------------------------

pytest = __import__("pytest", fromlist=["importorskip"])
pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("httpx", reason="httpx not installed")

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

try:
    import umsgpack  # type: ignore
    _UMSGPACK_AVAILABLE = True
except ImportError:
    _UMSGPACK_AVAILABLE = False

_SKIP_MSG = "fastapi / httpx / umsgpack not installed — skipping HAR AI tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal FastAPI app (no real DB needed for these endpoints)."""
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


def _make_auth_client(app, user_id=1, role="user"):
    """
    Create a TestClient that carries a valid 'user' secure cookie.
    Returns the TestClient with the cookie already set.
    """
    from fastapi import APIRouter, Response
    from web.fastapi.auth import set_secure_cookie

    helper = APIRouter()

    @helper.get("/_test_set_auth_har_ai")
    def _set(response: Response):
        payload = umsgpack.packb({"id": user_id, "role": role})
        set_secure_cookie(response, "user", payload)
        return {"ok": True}

    app.include_router(helper)
    client = TestClient(app, follow_redirects=False)
    r = client.get("/_test_set_auth_har_ai")
    assert r.status_code == 200, f"Cookie setup failed: {r.status_code}"
    return client


# ---------------------------------------------------------------------------
# Case 1 & 2: GET /har/ai_status
# ---------------------------------------------------------------------------


@unittest.skipUnless(_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE, _SKIP_MSG)
class TestHARAIStatus(unittest.TestCase):
    """Tests for GET /har/ai_status."""

    def setUp(self):
        self.app = _make_app()
        self.anon = TestClient(self.app, follow_redirects=False)
        self.auth = _make_auth_client(self.app)

    def test_case1_ai_status_anonymous_returns_401(self):
        """Case 1: GET /har/ai_status without auth → 401."""
        r = self.anon.get("/har/ai_status")
        self.assertEqual(r.status_code, 401)

    def test_case2_ai_status_authenticated_no_model_field(self):
        """Case 2: GET /har/ai_status authenticated → 200, no 'model' field."""
        with patch("web.fastapi.handlers.har_ai.ai_client.AIClient") as MockAI:
            inst = MagicMock()
            inst.enabled = False
            MockAI.return_value = inst
            r = self.auth.get("/har/ai_status")

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("enabled", body)
        # Security invariant: model name must NOT be exposed
        self.assertNotIn("model", body)

    def test_ai_status_enabled_true_when_key_set(self):
        """GET /har/ai_status returns enabled=True when AI key is present."""
        with patch("web.fastapi.handlers.har_ai.ai_client.AIClient") as MockAI:
            inst = MagicMock()
            inst.enabled = True
            MockAI.return_value = inst
            r = self.auth.get("/har/ai_status")

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["enabled"])
        self.assertNotIn("model", r.json())


# ---------------------------------------------------------------------------
# Case 3: GET /har/auto_capture_status
# ---------------------------------------------------------------------------


@unittest.skipUnless(_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE, _SKIP_MSG)
class TestHARAutoCaptureStatus(unittest.TestCase):
    """Tests for GET /har/auto_capture_status."""

    def setUp(self):
        self.app = _make_app()
        self.anon = TestClient(self.app, follow_redirects=False)
        self.auth = _make_auth_client(self.app)

    def test_case8_auto_capture_status_anonymous_returns_401(self):
        """Case 8: GET /har/auto_capture_status without auth → 401."""
        r = self.anon.get("/har/auto_capture_status")
        self.assertEqual(r.status_code, 401)

    def test_case3_auto_capture_status_no_sidecar_url(self):
        """Case 3: GET /har/auto_capture_status authenticated → 200, no 'sidecar_url'."""
        import config
        with patch.object(config, "playwright_sidecar_url", ""):
            r = self.auth.get("/har/auto_capture_status")

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("enabled", body)
        # Security invariant: sidecar_url must NOT be exposed
        self.assertNotIn("sidecar_url", body)
        self.assertFalse(body["enabled"])

    def test_case9_auto_capture_status_enabled_when_sidecar_configured(self):
        """Case 9: GET /har/auto_capture_status returns enabled=True with sidecar set."""
        import config
        with patch.object(config, "playwright_sidecar_url", "http://sidecar:3000"):
            r = self.auth.get("/har/auto_capture_status")

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertNotIn("sidecar_url", body)


# ---------------------------------------------------------------------------
# Cases 4–7: POST /har/ai_analyze
# ---------------------------------------------------------------------------


@unittest.skipUnless(_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE, _SKIP_MSG)
class TestHARAIAnalyze(unittest.TestCase):
    """Tests for POST /har/ai_analyze."""

    def setUp(self):
        self.app = _make_app()
        self.anon = TestClient(self.app, follow_redirects=False)
        self.auth = _make_auth_client(self.app)

    def test_case6_ai_analyze_anonymous_returns_401(self):
        """Case 6: POST /har/ai_analyze without auth → 401."""
        r = self.anon.post(
            "/har/ai_analyze",
            json={"har": {"log": {"entries": []}}, "hint": ""},
        )
        self.assertEqual(r.status_code, 401)

    def test_case4_ai_analyze_no_api_key_returns_503(self):
        """Case 4: POST /har/ai_analyze when AI not configured → 503."""
        with patch("web.fastapi.handlers.har_ai.ai_client.AIClient") as MockAI:
            inst = MagicMock()
            inst.enabled = False
            MockAI.return_value = inst
            r = self.auth.post(
                "/har/ai_analyze",
                json={"har": {"log": {"entries": []}}, "hint": ""},
            )

        self.assertEqual(r.status_code, 503)

    def test_case5_ai_analyze_missing_har_field_returns_400(self):
        """Case 5: POST /har/ai_analyze with no har field → 400."""
        with patch("web.fastapi.handlers.har_ai.ai_client.AIClient") as MockAI:
            inst = MagicMock()
            inst.enabled = True
            MockAI.return_value = inst
            r = self.auth.post("/har/ai_analyze", json={"hint": "test"})

        self.assertEqual(r.status_code, 400)

    def test_case7_ai_analyze_success_path(self):
        """Case 7: POST /har/ai_analyze with valid HAR and mocked AI → 200."""
        mock_out = {
            "result": [{"method": "GET", "url": "https://example.com/api"}],
            "har": {"log": {"entries": []}},
            "stats": {"input_entries": 1},
        }

        with patch("web.fastapi.handlers.har_ai.ai_client.AIClient") as MockAI, \
             patch(
                 "web.fastapi.handlers.har_ai._analyze_har_with_ai",
                 new=AsyncMock(return_value=mock_out),
             ):
            inst = MagicMock()
            inst.enabled = True
            MockAI.return_value = inst
            r = self.auth.post(
                "/har/ai_analyze",
                json={
                    "har": {"log": {"entries": [{"request": {"url": "https://example.com"}}]}},
                    "hint": "签到",
                },
            )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertIn("har", body)
        self.assertIn("result", body)
        self.assertIn("stats", body)


# ---------------------------------------------------------------------------
# Cases 10–12: POST /har/auto_capture
# ---------------------------------------------------------------------------


@unittest.skipUnless(_FASTAPI_AVAILABLE and _UMSGPACK_AVAILABLE, _SKIP_MSG)
class TestHARAutoCapture(unittest.TestCase):
    """Tests for POST /har/auto_capture."""

    def setUp(self):
        self.app = _make_app()
        self.anon = TestClient(self.app, follow_redirects=False)
        self.auth = _make_auth_client(self.app)

    def test_case10_auto_capture_anonymous_returns_401(self):
        """Case 10: POST /har/auto_capture without auth → 401."""
        r = self.anon.post("/har/auto_capture", json={"url": "https://example.com"})
        self.assertEqual(r.status_code, 401)

    def test_case11_auto_capture_sidecar_not_configured_returns_503(self):
        """Case 11: POST /har/auto_capture when sidecar not configured → 503."""
        import config
        with patch.object(config, "playwright_sidecar_url", ""):
            r = self.auth.post("/har/auto_capture", json={"url": "https://example.com"})

        self.assertEqual(r.status_code, 503)

    def test_case12_auto_capture_missing_url_returns_400(self):
        """Case 12: POST /har/auto_capture without url field → 400."""
        import config
        with patch.object(config, "playwright_sidecar_url", "http://sidecar:3000"):
            r = self.auth.post("/har/auto_capture", json={"hint": "test"})

        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------------------
# Case 13: Route registration sanity check
# ---------------------------------------------------------------------------


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi not installed")
class TestHARAIRouteRegistration(unittest.TestCase):
    """Case 13: All four HAR AI routes are registered in the FastAPI app."""

    def test_all_four_routes_registered(self):
        from web.fastapi_app import create_app
        app = create_app(db=None, fetcher=None, version="test")
        paths = {r.path for r in app.routes}
        self.assertIn("/har/ai_analyze", paths)
        self.assertIn("/har/ai_status", paths)
        self.assertIn("/har/auto_capture", paths)
        self.assertIn("/har/auto_capture_status", paths)


if __name__ == "__main__":
    unittest.main()
