"""Remove credential values before building a model prompt from an imported HAR."""

import re
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

FIELD = r"(?:[\w.-]*?(?:password|passwd|passcode|secret|token|csrf|cookie|session|captcha|authorization|api[_-]?key|username|email|phone|mobile|credential|otp|sms[_-]?code)[\w.-]*|pwd|auth|sid|code|pin)"
SENSITIVE = re.compile(rf"^{FIELD}$", re.I)
AUTH_ROUTE = re.compile(
    r"(?i)(?:^|[/_.-])(?:oauth\w*|sso|passport|login|log[-_]?in|captcha|challenge|sms|otp|qrconnect)(?:$|[/_.-])"
)
VALUE_PAIR = re.compile(
    rf"(?i)(?<![\w.-])(?P<prefix>[\"']?(?P<name>{FIELD})[\"']?\s*[:=]\s*)"
    r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,&;<>}\]"']+)"""
)
AUTH_VALUE = re.compile(r"(?i)\b(?:Bearer|Basic|Digest)\s+[^\s,;\"'<>]+")
PERSONAL_VALUE = re.compile(r"(?i)[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b1[3-9]\d{9}\b|\bsk-[\w-]{8,}")
OPAQUE_VALUE = re.compile(r"\b(?=[A-Za-z0-9_=-]*\d)(?=[A-Za-z0-9_=-]*[A-Za-z])[A-Za-z0-9_=-]{40,}\b")
HTML_INPUT = re.compile(r"(?i)<input\b[^>]{0,1200}>")
PRIVATE_INPUT = re.compile(r"(?i)password|hidden|autocomplete|token|csrf|captcha|email|phone")


def sensitive(name):
    return bool(SENSITIVE.fullmatch(name or ""))


def placeholder(name):
    safe_name = re.sub(r"\W+", "_", name.lower()).strip("_")
    return "{{" + safe_name + "}}"


def redact_text(text, known=()):
    def without_raw_values(value):
        for raw in known:
            value = value.replace(raw, "[REDACTED]")
        return value

    def replace_pair(match):
        val = match["value"]
        plain = val.strip("\"'")
        if val in ("true", "false", "null") or (
            match["name"].lower() == "code" and plain.isdigit() and len(plain) < 4
        ):
            return match[0]
        mark = placeholder(match["name"])
        if val.startswith(('"', "'")):
            mark = val[0] + mark + val[0]
        return match["prefix"] + mark

    text = VALUE_PAIR.sub(replace_pair, text)
    text = HTML_INPUT.sub(
        lambda match: "<input [REDACTED]>" if PRIVATE_INPUT.search(match[0]) else match[0], text
    )
    text = AUTH_VALUE.sub(lambda m: m[0].split()[0] + " {{token}}", text)
    return without_raw_values(OPAQUE_VALUE.sub("[REDACTED]", PERSONAL_VALUE.sub("[REDACTED]", text)))


def redact_url(url, known):
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    query = []
    for item in parts.query.split("&") if parts.query else ():
        key, separator, value = item.partition("=")
        name = unquote_plus(key)
        clean_name = redact_text(name, known)
        decoded = unquote_plus(value)
        clean_value = placeholder(name) if sensitive(name) else redact_text(decoded, known)
        query.append(
            (key if clean_name == name else clean_name)
            + separator
            + (value if clean_value == decoded else clean_value)
        )

    def safe_segment(segment):
        decoded = unquote(segment)
        clean = redact_text(decoded, known)
        return segment if decoded == clean else clean

    path = "/".join(safe_segment(segment) for segment in parts.path.split("/"))
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], path, "&".join(query), ""))


def known_credentials(entries):
    credentials = set()

    def add(value):
        if isinstance(value, str) and 4 <= len(value) <= 1024 and len(credentials) < 200:
            credentials.update((value, quote(value, safe=""), quote_plus(value)))

    for entry in entries[:500]:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request", {}) or {}
        if not isinstance(req, dict):
            continue
        for header in req.get("headers", []) or []:
            name = (header.get("name") or "").lower()
            value = header.get("value") or ""
            if name == "cookie":
                for part in value.split(";"):
                    add(part.partition("=")[2].strip())
            elif sensitive(name):
                add(value.split(None, 1)[-1] if value.strip() and name == "authorization" else value)
        for item in req.get("cookies", []) or []:
            add(item.get("value"))
        try:
            query = urlsplit(req.get("url", "")).query
        except ValueError:
            query = ""
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if sensitive(unquote_plus(key)):
                add(unquote_plus(value))
        data = req.get("postData", {}) or {}
        for item in data.get("params", []) or []:
            if sensitive(item.get("name")):
                add(item.get("value"))
        for match in VALUE_PAIR.finditer((data.get("text") or "")[:8192]):
            name = match["name"].lower()
            value = match["value"].strip("\"'")
            if name != "code" or not value.isdigit() or len(value) >= 4:
                add(unquote_plus(value))
    return sorted(credentials, key=len, reverse=True)
