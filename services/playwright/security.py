"""Sidecar 安全工具: cookie 解析 + storage_state 跨域剔除。

抽成独立模块, 与 fastapi/playwright 解耦, 便于单元测试。
"""

from __future__ import annotations

import logging
from typing import Any, Dict
from urllib.parse import urlparse

logger = logging.getLogger("qd.playwright.security")


def parse_cookie_str_to_storage_state(cookie_str: str, url: str) -> Dict[str, Any]:
    """把简单 'k1=v1; k2=v2' 转换为 Playwright storage_state cookies 列表。

    限制: 此简单解析按 ';' 切分, 因此 cookie 值内不能含 ';'。
    如果用户的 cookie 值含分号, 应改用 storage_state JSON 字段提供完整结构。
    """
    parsed = urlparse(url)
    domain = parsed.hostname or ""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": "." + domain if not domain.startswith(".") else domain,
                "path": "/",
                "httpOnly": False,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            }
        )
    return {"cookies": cookies, "origins": []}


def domain_matches(cookie_domain: str, request_host: str) -> bool:
    """cookie domain 是否属于 request URL host (含父域)。

    安全说明
    --------
    * ``cookie_domain`` 为空字符串、None 或仅由 '.' 组成时直接返回 False，
      防止空 domain 绕过剔除逻辑。
    * 匹配采用严格后缀规则（rh == cd 或 rh.endswith("." + cd)），
      杜绝 evil.com 匹配 notevil.com 的前缀注入攻击。
    """
    if not cookie_domain or not request_host:
        return False
    # Coerce non-str types (e.g. None stored as JSON null already handled above,
    # but guard against any stray non-string value coming from user-supplied JSON)
    if not isinstance(cookie_domain, str) or not isinstance(request_host, str):
        return False
    cd = cookie_domain.lstrip(".").lower()
    if not cd:
        # domain was purely dots — reject
        return False
    rh = request_host.lower()
    return rh == cd or rh.endswith("." + cd)


def sanitize_storage_state(state: Dict[str, Any], url: str) -> Dict[str, Any]:
    """剔除与 URL host 不匹配的 cookie / origin, 防止用户 (恶意或粗心)
    把跨域凭据塞进来导致 sidecar 在我方代为携带其它站点 cookie。

    返回值会替换原 state, 不修改入参。
    """
    request_host = urlparse(url).hostname or ""
    safe_cookies = []
    dropped_cookies = []
    for c in (state.get("cookies") or []):
        cd = c.get("domain", "")
        if domain_matches(cd, request_host):
            safe_cookies.append(c)
        else:
            dropped_cookies.append(cd)
    safe_origins = []
    dropped_origins = []
    for o in (state.get("origins") or []):
        origin = (o.get("origin") or "").lower()
        ohost = urlparse(origin).hostname or ""
        if ohost and domain_matches(ohost, request_host):
            safe_origins.append(o)
        else:
            dropped_origins.append(origin)
    if dropped_cookies or dropped_origins:
        logger.warning(
            "storage_state 剔除了与 %s 不匹配的 cookies=%s origins=%s",
            request_host,
            dropped_cookies[:5],
            dropped_origins[:5],
        )
    return {"cookies": safe_cookies, "origins": safe_origins}
