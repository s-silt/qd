#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Tornado-compatible secure-cookie implementation for FastAPI.

Implements the Tornado v2 secure cookie format exactly, matching Tornado 6.x.
Cookies written by this module can be read by Tornado and vice-versa, so
existing user sessions survive a switch to the FastAPI endpoint.

Tornado v2 wire format (bytes):
    b"2|" + format_field(key_version) + b"|"
          + format_field(timestamp_secs) + b"|"
          + format_field(name_bytes) + b"|"
          + format_field(base64(value)) + b"|"
          + hex_hmac_sha256(all_of_the_above_including_final_pipe)

Where ``format_field(s)`` = ``b"%d:%s" % (len(s), s)``

The HMAC key is ``cookie_secret`` (bytes) and is applied over the entire
byte string up to and **including** the final ``|`` separator.

References
----------
https://github.com/tornadoweb/tornado/blob/master/tornado/web.py
  - ``create_signed_value``  (v2 branch)
  - ``_create_signature_v2``
  - ``_decode_fields_v2``
  - ``_decode_signed_value_v2``
"""

import base64
import hashlib
import hmac
import time
from typing import Optional

import config

# ---------------------------------------------------------------------------
# Low-level helpers — exactly mirror Tornado internals
# ---------------------------------------------------------------------------


def _utf8(s) -> bytes:
    """Encode str → UTF-8 bytes; pass through bytes unchanged."""
    if isinstance(s, bytes):
        return s
    return s.encode("utf-8")


def _create_signature_v2(secret: bytes, s: bytes) -> bytes:
    """
    HMAC-SHA256 over ``s``, keyed by ``secret``.

    Returns hex digest as bytes (e.g. b"a1b2c3...").
    Matches ``tornado.web._create_signature_v2``.
    """
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    mac.update(_utf8(s))
    return _utf8(mac.hexdigest())


def _format_field(s) -> bytes:
    """
    Tornado v2 ``format_field``: produce b"<len>:<value>".

    ``s`` may be str, bytes, or int.
    """
    s_bytes = _utf8(str(s) if isinstance(s, int) else s)
    return _utf8(str(len(s_bytes))) + b":" + s_bytes


# ---------------------------------------------------------------------------
# Create / decode signed value (Tornado v2 format)
# ---------------------------------------------------------------------------


def create_signed_value(
    name: str,
    value: bytes,
    secret: Optional[bytes] = None,
    key_version: int = 0,
) -> str:
    """
    Produce a signed cookie value string in Tornado v2 format.

    Returns the ASCII string that should be stored as the cookie value.
    Compatible with ``tornado.web.create_signed_value(secret, name, value)``.

    Parameters
    ----------
    name:
        Cookie name (stored as a length-prefixed field in the signed data).
    value:
        Raw bytes payload to sign and encode.
    secret:
        HMAC key; defaults to ``config.cookie_secret``.
    key_version:
        Key version number (0 for the default key; relevant when using a
        key dictionary with Tornado's key rotation feature).
    """
    if secret is None:
        secret = config.cookie_secret

    timestamp = str(int(time.time()))
    value_b64 = base64.b64encode(value)

    # Build the bytes string exactly as Tornado does:
    # b"2|" + format_field(key_version) + "|"
    #       + format_field(timestamp)   + "|"
    #       + format_field(name)        + "|"
    #       + format_field(value_b64)   + "|"
    to_sign = (
        b"2|"
        + _format_field(str(key_version)) + b"|"
        + _format_field(timestamp) + b"|"
        + _format_field(name) + b"|"
        + _format_field(value_b64) + b"|"
    )

    sig = _create_signature_v2(secret, to_sign)
    return (to_sign + sig).decode("ascii")


def decode_signed_value(
    name: str,
    value: str,
    max_age_days: float = 31,
    secret: Optional[bytes] = None,
) -> Optional[bytes]:
    """
    Decode and verify a Tornado v2 secure cookie value.

    Returns the original bytes payload, or None if invalid / expired / tampered.
    Compatible with cookies written by Tornado's ``set_secure_cookie()``.

    Parameters
    ----------
    name:
        The cookie name (must match the name embedded in the signed value).
    value:
        The raw cookie string as received from the browser.
    max_age_days:
        Maximum age in days; cookies older than this are rejected.
    secret:
        HMAC key; defaults to ``config.cookie_secret``.
    """
    if secret is None:
        secret = config.cookie_secret

    if not value:
        return None

    raw = _utf8(value)

    # Must start with b"2|" (version 2)
    if not raw.startswith(b"2|"):
        return None

    def _consume_field(s: bytes):
        """Read one length-prefixed field: b"<n>:<value>|<rest>" -> (field, rest)."""
        colon_pos = s.index(b":")
        n = int(s[:colon_pos])
        start = colon_pos + 1
        field = s[start: start + n]
        rest = s[start + n:]
        if not rest.startswith(b"|"):
            raise ValueError("malformed v2 field: expected pipe after field")
        return field, rest[1:]

    try:
        rest = raw[2:]  # strip b"2"
        _key_version_field, rest = _consume_field(rest)
        timestamp_field, rest = _consume_field(rest)
        name_field, rest = _consume_field(rest)
        value_field, passed_sig = _consume_field(rest)
    except Exception:
        return None

    # The signed portion = everything up to (not including) the signature bytes.
    signed_portion = raw[: -len(passed_sig)]

    expected_sig = _create_signature_v2(secret, signed_portion)
    if not hmac.compare_digest(passed_sig, expected_sig):
        return None

    if name_field != _utf8(name):
        return None

    try:
        ts = int(timestamp_field)
    except (ValueError, TypeError):
        return None

    age_secs = time.time() - ts
    if age_secs < 0 or age_secs > max_age_days * 86400:
        return None

    try:
        return base64.b64decode(value_field)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# FastAPI request / response helpers
# ---------------------------------------------------------------------------


def set_secure_cookie(
    response,
    name: str,
    value: bytes,
    expires_days: int = 30,
    secret: Optional[bytes] = None,
) -> None:
    """
    Set a Tornado-compatible signed cookie on a FastAPI Response object.

    Usage::

        from fastapi import Response
        from web.fastapi.auth import set_secure_cookie

        @router.post("/login")
        async def login(response: Response):
            set_secure_cookie(response, "user", umsgpack.packb(user_dict))
    """
    signed = create_signed_value(name, value, secret)
    max_age = expires_days * 86400
    response.set_cookie(
        key=name,
        value=signed,
        max_age=max_age,
        httponly=True,
        secure=config.cookie_secure_mode,
        samesite="lax",
    )


def get_secure_cookie(
    request,
    name: str,
    max_age_days: float = 31,
    secret: Optional[bytes] = None,
) -> Optional[bytes]:
    """
    Read and verify a Tornado-compatible signed cookie from a FastAPI Request.

    Returns the raw bytes payload or None if absent / invalid / expired.
    """
    raw = request.cookies.get(name)
    if not raw:
        return None
    return decode_signed_value(name, raw, max_age_days=max_age_days, secret=secret)
