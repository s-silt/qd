"""Sidecar 安全工具: cookie 解析 + storage_state 跨域剔除。

抽成独立模块, 与 fastapi/playwright 解耦, 便于单元测试。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("qd.playwright.security")

# 是否同时拦截 RFC1918 私网地址 (10/8, 172.16/12, 192.168/16)。
# 本项目常部署于内网, 签到目标本身可能就是私网主机, 故默认放行私网,
# 仅拦截环回 / 链路本地(含云元数据 169.254.169.254) / 多播 / 保留地址。
# 在公网部署时可设置 PLAYWRIGHT_BLOCK_PRIVATE_IP=1 收紧。
BLOCK_PRIVATE_IP = os.getenv("PLAYWRIGHT_BLOCK_PRIVATE_IP", "").lower() in (
    "1",
    "true",
    "yes",
)


# 仅由数字 / 十六进制 / 点组成的 host 可能是十进制 / 八进制 / 十六进制 / 短写
# 的 IPv4 表示 (如 2130706433 / 0x7f000001 / 0177.0.0.1 / 127.1)。这些写法
# ipaddress.ip_address() 不接受, 但 socket.inet_aton() (= libc inet_addr) 会
# 解析, 攻击者借此绕过 SSRF 守卫直达 127.0.0.1 / 169.254.169.254。
_NUMERIC_IPV4_RE = re.compile(r"^[0-9a-fA-FxX.]+$")


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    # IPv6 内可能内嵌 IPv4 (::ffff:127.0.0.1 映射 / 2002::/16 6to4),
    # 先按内嵌的 IPv4 判一次, 防止经映射地址绕过环回 / 元数据拦截。
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour):
            if embedded is not None and _ip_is_blocked(embedded):
                return True
    if (
        ip.is_loopback           # 127.0.0.0/8 全段 + ::1
        or ip.is_link_local      # 169.254.0.0/16 (云元数据) + fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified     # 0.0.0.0 + ::
    ):
        return True
    # 私网 (RFC1918 / IPv6 唯一本地 fc00::/7) 默认放行 (内网部署),
    # 仅在显式收紧时拦截。
    if BLOCK_PRIVATE_IP and ip.is_private:
        return True
    return False


def _parse_ip_literal(host: str) -> Optional[ipaddress._BaseAddress]:
    """把 host 解析为 IP 对象, 覆盖十进制 / 八进制 / 十六进制 / 短写 IPv4 写法。

    标准点分 / IPv6 字面量解析失败时, 对"看起来是数字 IP"的 host 再尝试
    inet_aton, 以规范化绕过写法。普通域名不匹配数字字符集, 直接返回 None。
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _NUMERIC_IPV4_RE.match(host):
        try:
            packed = socket.inet_aton(host)
            return ipaddress.ip_address(packed)
        except OSError:
            pass
    return None


def resolve_blocked_reason(host: str) -> str:
    """检查 host (IP 字面量或域名) 是否解析到被禁止的地址段。

    返回拦截原因字符串; 放行时返回空字符串。
    用于防 SSRF: 阻止 sidecar 访问环回 / 云元数据 / 多播等内部地址。
    """
    if not host:
        return "缺少 hostname"
    host = host.strip("[]")  # 去掉 IPv6 字面量方括号

    # 1. 直接是 IP 字面量 (含十进制 / 八进制 / 十六进制 / 短写 IPv4 绕过写法)
    ip = _parse_ip_literal(host)
    if ip is not None:
        return "目标地址属于受限网段" if _ip_is_blocked(ip) else ""

    # 2. 域名: 尽力解析所有 A/AAAA 记录, 任一落入受限段即拦截 (防 DNS rebinding)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # 解析失败交给后续真实请求处理, 此处不误杀
        return ""
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if _ip_is_blocked(ip):
            return f"域名 {host} 解析到受限地址 {addr}"
    return ""


def resolve_url_blocked_reason(url: str) -> str:
    """给定完整 URL → 是否应拦截 + 原因 的统一入口 (供 fetcher/playwright/ocr 复用)。

    从 URL 解析出 hostname 后委托给 resolve_blocked_reason; 解析不出 host
    (如非法 URL / 缺少 scheme) 时按"缺少 hostname"拦截。
    返回拦截原因字符串; 放行时返回空字符串。
    """
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "无法解析的 URL"
    if not host:
        return "缺少 hostname"
    return resolve_blocked_reason(host)


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
    """cookie domain 是否属于 request URL host (含父域)。"""
    if not cookie_domain or not request_host:
        return False
    cd = cookie_domain.lstrip(".").lower()
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
