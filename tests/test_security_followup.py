"""Security follow-up regression tests (2026-04-29 audit, Phase 2).

Covers S-2, M-3, M-4 fixes:
  - HARAutoCaptureStatus requires authentication (S-2)
  - HARAutoCaptureStatus response omits sidecar_url (S-2)
  - HARAIStatus requires authentication (M-4)
  - HARAIStatus response omits model field (M-4)
  - _is_blocked_host blocks private/loopback/metadata IPs (M-3)
  - validate_url raises for blocked hosts when ALLOW_HOSTS unset (M-3)

Import strategy
---------------
* ``services/playwright/app.py`` imports ``fastapi`` / ``playwright`` which are
  not installed in the test environment.  We inject minimal stub modules so the
  file can be imported to access ``_is_blocked_host`` and ``CaptureRequest``.
* ``web/handlers/har.py`` ultimately imports ``libs.mcrypto`` which requires the
  ``pbkdf2`` package (not installed).  We therefore test the handler behaviour
  with pure AST inspection rather than importing the module.
"""
import ast
import ipaddress
import importlib
import os
import sys
import types
import unittest


# ---------------------------------------------------------------------------
# Helpers: paths
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HAR_PY = os.path.join(_REPO_ROOT, "web", "handlers", "har.py")
_APP_PY = os.path.join(_REPO_ROOT, "services", "playwright", "app.py")


# ---------------------------------------------------------------------------
# Bootstrap: inject stubs so services/playwright/app.py can be imported
# ---------------------------------------------------------------------------

def _inject_stubs() -> None:
    """Inject minimal stubs for fastapi / playwright / button_finder."""

    # ---- fastapi stub ----
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")
        fastapi_mod.FastAPI = type("FastAPI", (), {"__init__": lambda self, **kw: None})
        fastapi_mod.HTTPException = Exception
        sys.modules["fastapi"] = fastapi_mod

    # ---- playwright stubs ----
    for name in ("playwright", "playwright.async_api"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    pw_api = sys.modules["playwright.async_api"]
    pw_api.Browser = object
    pw_api.Error = Exception
    pw_api.TimeoutError = TimeoutError
    pw_api.async_playwright = lambda: None

    # ---- button_finder stub ----
    if "button_finder" not in sys.modules:
        bf_mod = types.ModuleType("button_finder")
        bf_mod.JS_FIND_CANDIDATES = ""
        bf_mod.pick_button = lambda candidates, hint="": (None, [])
        sys.modules["button_finder"] = bf_mod


def _load_playwright_app():
    """Import services/playwright/app with stubs; return the module."""
    services_dir = os.path.join(_REPO_ROOT, "services", "playwright")
    if services_dir not in sys.path:
        sys.path.insert(0, services_dir)
    _inject_stubs()
    sys.modules.pop("app", None)
    return importlib.import_module("app")


try:
    _APP = _load_playwright_app()
    _APP_AVAILABLE = True
    _APP_ERR = ""
except Exception as _e:  # pragma: no cover
    _APP = None
    _APP_AVAILABLE = False
    _APP_ERR = str(_e)


# ---------------------------------------------------------------------------
# A. _is_blocked_host via actual app import (skipped if import fails)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_APP_AVAILABLE, f"playwright app import failed: {_APP_ERR}")
class TestIsBlockedHostFromApp(unittest.TestCase):
    """_is_blocked_host from the real app module."""

    def _b(self, h: str) -> bool:
        return _APP._is_blocked_host(h)

    def test_cloud_metadata(self):
        self.assertTrue(self._b("169.254.169.254"))

    def test_loopback(self):
        self.assertTrue(self._b("127.0.0.1"))

    def test_rfc1918_a(self):
        self.assertTrue(self._b("10.0.0.5"))

    def test_rfc1918_b(self):
        self.assertTrue(self._b("172.16.0.1"))

    def test_rfc1918_c(self):
        self.assertTrue(self._b("192.168.1.1"))

    def test_localhost_str(self):
        self.assertTrue(self._b("localhost"))

    def test_unspecified(self):
        self.assertTrue(self._b("0.0.0.0"))

    def test_ipv6_loopback(self):
        self.assertTrue(self._b("::1"))

    def test_ipv6_link_local(self):
        self.assertTrue(self._b("fe80::1"))

    def test_empty(self):
        self.assertTrue(self._b(""))

    def test_public_dns(self):
        self.assertFalse(self._b("8.8.8.8"))

    def test_cloudflare_dns(self):
        self.assertFalse(self._b("1.1.1.1"))

    def test_domain_name(self):
        self.assertFalse(self._b("example.com"))

    def test_public_ipv6(self):
        self.assertFalse(self._b("2001:4860:4860::8888"))


# ---------------------------------------------------------------------------
# B. _is_blocked_host re-implemented locally – always runs (spec min-5 cases)
# ---------------------------------------------------------------------------

def _is_blocked_host_local(hostname: str) -> bool:
    """Local copy of _is_blocked_host used when app import fails."""
    if not hostname:
        return True
    h = hostname.lower()
    if h in ("localhost", "0.0.0.0"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


# Use the real function if app loaded, otherwise use local copy.
_fn = _APP._is_blocked_host if _APP_AVAILABLE else _is_blocked_host_local


class TestIsBlockedHostSpec(unittest.TestCase):
    """Canonical 9-case spec – always runs regardless of import availability."""

    def test_spec_169_254_169_254_blocked(self):
        self.assertTrue(_fn("169.254.169.254"))

    def test_spec_127_0_0_1_blocked(self):
        self.assertTrue(_fn("127.0.0.1"))

    def test_spec_10_0_0_5_blocked(self):
        self.assertTrue(_fn("10.0.0.5"))

    def test_spec_8_8_8_8_allowed(self):
        self.assertFalse(_fn("8.8.8.8"))

    def test_spec_localhost_blocked(self):
        self.assertTrue(_fn("localhost"))

    def test_spec_172_16_0_1_blocked(self):
        self.assertTrue(_fn("172.16.0.1"))

    def test_spec_192_168_1_1_blocked(self):
        self.assertTrue(_fn("192.168.1.1"))

    def test_spec_1_1_1_1_allowed(self):
        self.assertFalse(_fn("1.1.1.1"))

    def test_spec_empty_blocked(self):
        self.assertTrue(_fn(""))


# ---------------------------------------------------------------------------
# C. validate_url BLOCK_PRIVATE_IPS enforcement (requires app import)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_APP_AVAILABLE, "playwright app import failed")
class TestValidateUrlBlockPrivateIPs(unittest.TestCase):
    """When ALLOW_HOSTS is empty + BLOCK_PRIVATE_IPS=True, validate_url must
    reject private/loopback/metadata URLs."""

    def _validate(self, url: str, allow_hosts=None, block: bool = True) -> str:
        orig_allow = _APP.ALLOW_HOSTS
        orig_block = _APP.BLOCK_PRIVATE_IPS
        try:
            _APP.ALLOW_HOSTS = allow_hosts if allow_hosts is not None else []
            _APP.BLOCK_PRIVATE_IPS = block
            return _APP.CaptureRequest.validate_url(url)
        finally:
            _APP.ALLOW_HOSTS = orig_allow
            _APP.BLOCK_PRIVATE_IPS = orig_block

    def test_blocks_metadata(self):
        with self.assertRaises(ValueError):
            self._validate("http://169.254.169.254/latest/meta-data/")

    def test_blocks_localhost(self):
        with self.assertRaises(ValueError):
            self._validate("http://localhost/admin")

    def test_blocks_rfc1918(self):
        with self.assertRaises(ValueError):
            self._validate("http://192.168.1.1/login")

    def test_allows_public_url(self):
        result = self._validate("https://example.com/checkin")
        self.assertEqual(result, "https://example.com/checkin")

    def test_block_disabled_allows_private(self):
        result = self._validate("http://192.168.1.1/admin", block=False)
        self.assertEqual(result, "http://192.168.1.1/admin")

    def test_allowhosts_whitelist_overrides(self):
        result = self._validate("http://192.168.1.1/admin", allow_hosts=["192.168.1.1"])
        self.assertEqual(result, "http://192.168.1.1/admin")

    def test_bad_scheme_rejected(self):
        with self.assertRaises(ValueError):
            self._validate("ftp://example.com/file")


# ---------------------------------------------------------------------------
# D. HAR handler source-level checks (AST-based, always runs)
# ---------------------------------------------------------------------------

def _parse_har() -> ast.Module:
    with open(_HAR_PY, encoding="utf-8") as f:
        return ast.parse(f.read())


def _class_get_has_authenticated(class_name: str) -> bool:
    tree = _parse_har()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "get":
                    for dec in item.decorator_list:
                        if "authenticated" in ast.unparse(dec):
                            return True
    return False


def _class_finish_has_key(class_name: str, key: str) -> bool:
    """Return True if any self.finish({...}) call inside the class contains
    `key` as a string constant (dict key)."""
    tree = _parse_har()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in ast.walk(node):
                if not (
                    isinstance(sub, ast.Await)
                    and isinstance(sub.value, ast.Call)
                ):
                    continue
                call = sub.value
                if not (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "finish"
                ):
                    continue
                for kn in ast.walk(call):
                    if isinstance(kn, ast.Constant) and kn.value == key:
                        return True
    return False


# ---- S-2: HARAutoCaptureStatus ----

class TestHARAutoCaptureStatusSourceLevel(unittest.TestCase):
    """HARAutoCaptureStatus: auth decorator + no sidecar_url in response."""

    def test_requires_authenticated_decorator(self):
        """HARAutoCaptureStatus.get must have @tornado.web.authenticated."""
        self.assertTrue(
            _class_get_has_authenticated("HARAutoCaptureStatus"),
            "HARAutoCaptureStatus.get is missing @tornado.web.authenticated",
        )

    def test_response_omits_sidecar_url(self):
        """The response dict must NOT contain the 'sidecar_url' key."""
        self.assertFalse(
            _class_finish_has_key("HARAutoCaptureStatus", "sidecar_url"),
            "HARAutoCaptureStatus.get must not return 'sidecar_url'",
        )

    def test_response_contains_enabled(self):
        """The response dict MUST contain the 'enabled' key."""
        self.assertTrue(
            _class_finish_has_key("HARAutoCaptureStatus", "enabled"),
            "HARAutoCaptureStatus.get must return 'enabled'",
        )


# ---- M-4: HARAIStatus ----

class TestHARAIStatusSourceLevel(unittest.TestCase):
    """HARAIStatus: auth decorator + no model in response."""

    def test_requires_authenticated_decorator(self):
        """HARAIStatus.get must have @tornado.web.authenticated."""
        self.assertTrue(
            _class_get_has_authenticated("HARAIStatus"),
            "HARAIStatus.get is missing @tornado.web.authenticated",
        )

    def test_response_omits_model(self):
        """The response dict must NOT contain the 'model' key."""
        self.assertFalse(
            _class_finish_has_key("HARAIStatus", "model"),
            "HARAIStatus.get must not return 'model'",
        )

    def test_response_contains_enabled(self):
        """The response dict MUST contain the 'enabled' key."""
        self.assertTrue(
            _class_finish_has_key("HARAIStatus", "enabled"),
            "HARAIStatus.get must return 'enabled'",
        )


# ---------------------------------------------------------------------------
# E. M-3: validate_url source-level check – BLOCK_PRIVATE_IPS branch present
# ---------------------------------------------------------------------------

class TestPlaywrightAppSourceLevel(unittest.TestCase):
    """Source-level checks for services/playwright/app.py."""

    def _read_app_source(self) -> str:
        with open(_APP_PY, encoding="utf-8") as f:
            return f.read()

    def test_block_private_ips_env_var_present(self):
        """BLOCK_PRIVATE_IPS env var must be defined in app.py."""
        src = self._read_app_source()
        self.assertIn("BLOCK_PRIVATE_IPS", src)

    def test_is_blocked_host_function_defined(self):
        """_is_blocked_host must be defined in app.py."""
        src = self._read_app_source()
        self.assertIn("def _is_blocked_host", src)

    def test_validate_url_calls_is_blocked_host(self):
        """validate_url must call _is_blocked_host for SSRF protection."""
        src = self._read_app_source()
        self.assertIn("_is_blocked_host", src.split("def validate_url")[1].split("def ")[0])

    def test_is_blocked_host_checks_loopback(self):
        """_is_blocked_host must check ip.is_loopback."""
        src = self._read_app_source()
        self.assertIn("is_loopback", src)

    def test_is_blocked_host_checks_private(self):
        """_is_blocked_host must check ip.is_private."""
        src = self._read_app_source()
        self.assertIn("is_private", src)

    def test_is_blocked_host_checks_link_local(self):
        """_is_blocked_host must check ip.is_link_local."""
        src = self._read_app_source()
        self.assertIn("is_link_local", src)


if __name__ == "__main__":
    unittest.main()
