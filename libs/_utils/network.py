#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Network / IP helper functions extracted from libs/utils.py
# pylint: disable=broad-exception-raised

import ipaddress
import re
import socket
import struct
from typing import Union

import config
from libs.log import Log

logger_util = Log('QD.Http.Util').getlogger()


def ip2int(addr):
    try:
        return struct.unpack("!I", socket.inet_aton(addr))[0]
    except Exception as e:
        logger_util.debug(e, exc_info=config.traceback_print)
        return int(ipaddress.ip_address(addr))


def ip2varbinary(addr: str, version: int):
    if version == 4:
        return socket.inet_aton(addr)
    if version == 6:
        return socket.inet_pton(socket.AF_INET6, addr)


def is_lan(ip):
    try:
        return ipaddress.ip_address(ip.strip()).is_private
    except Exception as e:
        logger_util.debug(e, exc_info=config.traceback_print)
        return False


def int2ip(addr):
    try:
        return socket.inet_ntoa(struct.pack("!I", addr))
    except Exception as e:
        logger_util.debug(e, exc_info=config.traceback_print)
        return str(ipaddress.ip_address(addr))


def varbinary2ip(addr: Union[bytes, int, str]):
    if isinstance(addr, int):
        return int2ip(addr)
    if isinstance(addr, str):
        addr = addr.encode('utf-8')
    if len(addr) == 4:
        return socket.inet_ntop(socket.AF_INET, addr)
    if len(addr) == 16:
        return socket.inet_ntop(socket.AF_INET6, addr)


def is_ip(addr=None):
    if addr:
        p = re.compile(r'''
         ((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?) # IPv4
         |::ffff:(0:)?((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?) # IPv4 mapped / translated addresses
         # |fe80:(:[0-9a-fA-F]{1,4}){0,4}(%\w+)? # IPv6 link-local
         |([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4} # IPv6 full
         |(([0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4})?::(([0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4})? # IPv6 with ::
         ''', re.VERBOSE)
        tmp = p.match(addr)
        if tmp and tmp[0] == addr:
            if ':' in tmp[0]:
                return 6
            else:
                return 4
        else:
            return 0
    return 0


def urlmatch(url):
    reobj = re.compile(r"""(?xi)\A
                ([a-z][a-z0-9+\-.]*://)?                            # Scheme
                ([a-z0-9\-._~%]+                                    # domain or IPv4 host
                |\[[a-z0-9\-._~%!$&'()*+,;=:]+\])                   # IPv6+ host
                (:[0-9]+)? """                                      # :port
                       )
    match = reobj.search(url)
    return match.group()


def url_match_with_limit(url):
    ip_middle_octet = r"(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5]))"
    ip_last_octet = r"(?:\.(?:0|[1-9]\d?|1\d\d|2[0-4]\d|25[0-5]))"

    reobj = re.compile(  # noqa: W605
        r"^"
        # protocol identifier
        r"(?:(?:https?|ftp)://)"
        # user:pass authentication
        r"(?:[-a-z¡-￿0-9._~%!$&'()*+,;=:]+"
        r"(?::[-a-z0-9._~%!$&'()*+,;=:]*)?@)?"
        r"(?:"
        r"(?P<private_ip>"
        # IP address exclusion
        # private & local networks
        r"(?:(?:10|127)" + ip_middle_octet + r"{2}" + ip_last_octet + r")|"
        r"(?:(?:169\.254|192\.168)" + ip_middle_octet + ip_last_octet + r")|"
        r"(?:172\.(?:1[6-9]|2\d|3[0-1])" + ip_middle_octet + ip_last_octet + r"))"
        r"|"
        # private & local hosts
        r"(?P<private_host>"
        r"(?:localhost))"
        r"|"
        # IP address dotted notation octets
        # excludes loopback network 0.0.0.0
        # excludes reserved space >= 224.0.0.0
        # excludes network & broadcast addresses
        # (first & last IP address of each class)
        r"(?P<public_ip>"
        r"(?:[1-9]\d?|1\d\d|2[01]\d|22[0-3])"
        r"" + ip_middle_octet + r"{2}"
        r"" + ip_last_octet + r")"
        r"|"
        # IPv6 RegEx from https://stackoverflow.com/a/17871737
        r"\[("
        # 1:2:3:4:5:6:7:8
        r"([0-9a-fA-F]{1,4}:){7,7}[0-9a-fA-F]{1,4}|"
        # 1::                              1:2:3:4:5:6:7::
        r"([0-9a-fA-F]{1,4}:){1,7}:|"
        # 1::8             1:2:3:4:5:6::8  1:2:3:4:5:6::8
        r"([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|"
        # 1::7:8           1:2:3:4:5::7:8  1:2:3:4:5::8
        r"([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|"
        # 1::6:7:8         1:2:3:4::6:7:8  1:2:3:4::8
        r"([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|"
        # 1::5:6:7:8       1:2:3::5:6:7:8  1:2:3::8
        r"([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|"
        # 1::4:5:6:7:8     1:2::4:5:6:7:8  1:2::8
        r"([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|"
        # 1::3:4:5:6:7:8   1::3:4:5:6:7:8  1::8
        r"[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|"
        # ::2:3:4:5:6:7:8  ::2:3:4:5:6:7:8 ::8       ::
        r":((:[0-9a-fA-F]{1,4}){1,7}|:)|"
        # fe80::7:8%eth0   fe80::7:8%1
        # (link-local IPv6 addresses with zone index)
        r"fe80:(:[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|"
        r"::(ffff(:0{1,4}){0,1}:){0,1}"
        r"((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}"
        # ::255.255.255.255   ::ffff:255.255.255.255  ::ffff:0:255.255.255.255
        # (IPv4-mapped IPv6 addresses and IPv4-translated addresses)
        r"(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])|"
        r"([0-9a-fA-F]{1,4}:){1,4}:"
        r"((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}"
        # 2001:db8:3:4::192.0.2.33  64:ff9b::192.0.2.33
        # (IPv4-Embedded IPv6 Address)
        r"(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])"
        r")\]|"
        # host name
        r"(?:(?:(?:xn--[-]{0,2})|[a-z¡-￿\U00010000-\U0010ffff0-9]-?)*"
        r"[a-z¡-￿\U00010000-\U0010ffff0-9]+)"
        # domain name
        r"(?:\.(?:(?:xn--[-]{0,2})|[a-z¡-￿\U00010000-\U0010ffff0-9]-?)*"
        r"[a-z¡-￿\U00010000-\U0010ffff0-9]+)*"
        # TLD identifier
        r"(?:\.(?:(?:xn--[-]{0,2}[a-z¡-￿\U00010000-\U0010ffff0-9]{2,})|"
        r"[a-z¡-￿\U00010000-\U0010ffff]{2,}))"
        r")"
        # port number
        r"(?::\d{2,5})?"
        # resource path
        r"(?:/[-a-z¡-￿\U00010000-\U0010ffff0-9._~%!$&'()*+,;=:@/]*)?"
        # query string
        r"(?:\?\S*)?"
        # fragment
        r"(?:#\S*)?"
        r"$",
        re.UNICODE | re.IGNORECASE
    )

    match = reobj.search(url)
    if match:
        return match.group()
    return ''


def domain_match(domain):
    reobj = re.compile(
        r'^(?:[a-zA-Z0-9]'  # First character of the domain
        r'(?:[a-zA-Z0-9-_]{0,61}[A-Za-z0-9])?\.)'  # Sub domain + hostname
        r'+[A-Za-z0-9][A-Za-z0-9-_]{0,61}'  # First 61 characters of the gTLD
        r'[A-Za-z]$'  # Last character of the gTLD
    )

    match = reobj.search(domain)
    if match:
        return match.group()
    return ''
