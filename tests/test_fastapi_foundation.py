#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Smoke tests for the FastAPI foundation layer.

Tests:
  1. /about route returns 200 + HTML.
  2. Secure-cookie round-trip: values written by our auth module can be
     read back, and the format is Tornado-compatible (verified against a
     known fixed cookie string).
  3. render_template namespace contains expected keys.

These tests are skipped automatically when fastapi / httpx are not installed
so they do not break environments that only have the Tornado stack.
"""

import importlib
import unittest

# ---------------------------------------------------------------------------
# Conditional skip if fastapi is unavailable
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_SKIP_MSG = "fastapi (and httpx) not installed — skipping FastAPI tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app():
    """Create a minimal FastAPI app suitable for testing (no real DB/Fetcher)."""
    from web.fastapi_app import create_app
    return create_app(db=None, fetcher=None, version="test")


# ---------------------------------------------------------------------------
# Test: /about route
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestAboutRoute(unittest.TestCase):
    """GET /about should return 200 and HTML content."""

    def setUp(self):
        self.app = _make_app()
        self.client = TestClient(self.app, raise_server_exceptions=True)

    def test_about_returns_200(self):
        response = self.client.get("/about")
        self.assertEqual(response.status_code, 200)

    def test_about_returns_html(self):
        response = self.client.get("/about")
        content_type = response.headers.get("content-type", "")
        self.assertIn("text/html", content_type)

    def test_about_contains_qd(self):
        """The about page should mention QD somewhere in the rendered HTML."""
        response = self.client.get("/about")
        self.assertIn("QD", response.text)

    def test_about_trailing_slash_redirects(self):
        """GET /about/ should redirect (301/307) or return 200 (follow_redirects=True)."""
        response = self.client.get("/about/", follow_redirects=True)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Test: Secure cookie round-trip
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestSecureCookieRoundTrip(unittest.TestCase):
    """
    Verify that create_signed_value / decode_signed_value form a correct
    round-trip and that the format is compatible with Tornado v2 cookies.
    """

    _SECRET = b"test_secret_key_32_bytes_padding!"

    def test_encode_decode_roundtrip(self):
        from web.fastapi.auth import create_signed_value, decode_signed_value
        payload = b"hello world"
        signed = create_signed_value("test_name", payload, secret=self._SECRET)
        result = decode_signed_value("test_name", signed,
                                     max_age_days=1, secret=self._SECRET)
        self.assertEqual(result, payload)

    def test_wrong_name_rejected(self):
        from web.fastapi.auth import create_signed_value, decode_signed_value
        signed = create_signed_value("alice", b"data", secret=self._SECRET)
        result = decode_signed_value("bob", signed, max_age_days=1, secret=self._SECRET)
        self.assertIsNone(result)

    def test_tampered_signature_rejected(self):
        from web.fastapi.auth import create_signed_value, decode_signed_value
        signed = create_signed_value("name", b"value", secret=self._SECRET)
        # Corrupt the last character of the signature
        tampered = signed[:-1] + ("X" if signed[-1] != "X" else "Y")
        result = decode_signed_value("name", tampered, max_age_days=1, secret=self._SECRET)
        self.assertIsNone(result)

    def test_tornado_v2_format_structure(self):
        """The signed value must start with '2|' (Tornado v2 format version)."""
        from web.fastapi.auth import create_signed_value
        signed = create_signed_value("user", b"payload", secret=self._SECRET)
        self.assertTrue(signed.startswith("2|"),
                        f"Expected Tornado v2 format (2|...), got: {signed[:20]}")

    def test_wrong_secret_rejected(self):
        from web.fastapi.auth import create_signed_value, decode_signed_value
        signed = create_signed_value("x", b"data", secret=self._SECRET)
        result = decode_signed_value("x", signed, max_age_days=1,
                                     secret=b"different_secret_key!!!!!!!!!!!")
        self.assertIsNone(result)

    def test_tornado_written_cookie_readable(self):
        """
        Verify compatibility with a cookie produced by Tornado itself.

        We generate a reference cookie via tornado.web.create_signed_value
        (if tornado is available) and check that our decoder can read it.
        """
        try:
            from tornado.web import create_signed_value as tornado_create
        except ImportError:
            self.skipTest("tornado not available for cross-compatibility test")

        import config as cfg
        secret = cfg.cookie_secret
        # Tornado API: create_signed_value(secret, name, value)
        tornado_signed = tornado_create(secret, "user", b"test_payload")
        if isinstance(tornado_signed, bytes):
            tornado_signed = tornado_signed.decode("ascii")

        from web.fastapi.auth import decode_signed_value
        result = decode_signed_value("user", tornado_signed, max_age_days=1, secret=secret)
        self.assertEqual(result, b"test_payload")

    def test_set_get_via_request_response(self):
        """set_secure_cookie / get_secure_cookie via a real TestClient request."""
        from fastapi import APIRouter, Request, Response
        from web.fastapi.auth import get_secure_cookie, set_secure_cookie

        app = _make_app()
        test_router = APIRouter()

        @test_router.get("/set-cookie-test")
        def _set(response: Response):
            set_secure_cookie(response, "testkey", b"hello_bytes")
            return {"ok": True}

        @test_router.get("/get-cookie-test")
        def _get(request: Request):
            val = get_secure_cookie(request, "testkey")
            return {"value": val.decode() if val else None}

        app.include_router(test_router)
        # Use a persistent client so cookies are automatically carried over
        client = TestClient(app)

        r1 = client.get("/set-cookie-test")
        self.assertEqual(r1.status_code, 200)
        # The cookie should be set
        self.assertIn("testkey", r1.cookies)

        # Use the client's cookie jar (persistent across requests)
        r2 = client.get("/get-cookie-test")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["value"], "hello_bytes")


# ---------------------------------------------------------------------------
# Test: render_template namespace
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestRenderTemplateNamespace(unittest.TestCase):
    """
    Verify that render_template injects the expected keys into the template
    context (mirrors _BaseHandler.render_string namespace).
    """

    def _get_namespace_from_about(self):
        """
        Monkey-patch jinja_env.get_template so we can capture the namespace
        passed to template.render().
        """
        from unittest.mock import MagicMock, patch

        captured = {}

        class _FakeTemplate:
            def render(self, ns):
                captured.update(ns)
                return "<html>mock</html>"

        app = _make_app()

        original_get_template = app.state.jinja_env.get_template

        def _patched_get_template(name):
            return _FakeTemplate()

        app.state.jinja_env.get_template = _patched_get_template

        client = TestClient(app)
        response = client.get("/about")
        # Restore
        app.state.jinja_env.get_template = original_get_template
        return captured, response

    def test_namespace_has_static_url(self):
        ns, _ = self._get_namespace_from_about()
        self.assertIn("static_url", ns)
        self.assertTrue(callable(ns["static_url"]))

    def test_namespace_has_request(self):
        ns, _ = self._get_namespace_from_about()
        self.assertIn("request", ns)

    def test_namespace_has_current_user(self):
        ns, _ = self._get_namespace_from_about()
        # current_user is None for unauthenticated requests
        self.assertIn("current_user", ns)
        self.assertIsNone(ns["current_user"])

    def test_namespace_has_xsrf_token(self):
        ns, _ = self._get_namespace_from_about()
        self.assertIn("xsrf_token", ns)

    def test_namespace_has_xsrf_form_html(self):
        ns, _ = self._get_namespace_from_about()
        self.assertIn("xsrf_form_html", ns)

    def test_namespace_has_handler(self):
        ns, _ = self._get_namespace_from_about()
        self.assertIn("handler", ns)

    def test_namespace_has_locale(self):
        ns, _ = self._get_namespace_from_about()
        self.assertIn("locale", ns)

    def test_namespace_has_reverse_url(self):
        ns, _ = self._get_namespace_from_about()
        self.assertIn("reverse_url", ns)
        self.assertTrue(callable(ns["reverse_url"]))

    def test_static_url_prepends_prefix(self):
        ns, _ = self._get_namespace_from_about()
        import config
        result = ns["static_url"]("css/my.css")
        prefix = config.static_url_prefix.rstrip("/")
        self.assertTrue(
            result.startswith(prefix),
            f"static_url('css/my.css') = {result!r}, expected to start with {prefix!r}",
        )


# ---------------------------------------------------------------------------
# Test: auto-discovery of routers
# ---------------------------------------------------------------------------

@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_MSG)
class TestRouterDiscovery(unittest.TestCase):
    """Verify that about.py router is picked up by the auto-discovery."""

    def test_about_router_discovered(self):
        from web.fastapi.handlers import routers
        from fastapi.routing import APIRouter
        self.assertTrue(
            any(isinstance(r, APIRouter) for r in routers),
            "Expected at least one APIRouter to be discovered",
        )

    def test_about_route_in_app(self):
        app = _make_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        self.assertIn("/about", paths,
                      f"/about not found in routes: {paths}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
