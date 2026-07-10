#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Jinja2 globals / filters and associated helper functions extracted from libs/utils.py
# pylint: disable=broad-exception-raised

import codecs
import html
import random
import re
import time
from typing import Any, Iterable, Mapping, Tuple, Union
from urllib import parse as urllib_parse

import charset_normalizer
from faker import Faker
from jinja2.filters import do_float, do_int
from jinja2.runtime import Undefined
from jinja2.utils import generate_lorem_ipsum, url_quote
from requests.utils import get_encoding_from_headers

import config
from libs.convert import to_bytes, to_native, to_text
from libs.log import Log
from libs._utils.crypto import (
    b64decode, b64encode, get_encrypted_password, get_hash,
    md5string, secure_hash_s, to_uuid, _aes_decrypt, _aes_encrypt,
)
from libs._utils.datetime_fmt import get_date_time, timestamp

logger_util = Log('QD.Http.Util').getlogger()


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def utf8(value):
    if isinstance(value, str):
        return value.encode('utf8')
    return value


def conver2unicode(value, html_unescape=False):
    if not isinstance(value, str):
        try:
            value = value.decode()
        except Exception as e:
            logger_util.debug(e, exc_info=config.traceback_print)
            value = str(value)
    tmp = bytes(value, 'unicode_escape').decode('utf-8').replace(r'\u', r'\\u').replace(r'\\\u', r'\\u')
    tmp = bytes(tmp, 'utf-8').decode('unicode_escape')
    tmp = tmp.encode('utf-8').replace(b'\xc2\xa0', b'\xa0').decode('unicode_escape')
    if html_unescape:
        tmp = html.unescape(tmp)
    return tmp


def urlencode_with_encoding(
    value: Union[str, Mapping[str, Any], Iterable[Tuple[str, Any]]],
    encoding: str = "utf-8",
    for_qs: bool = False,
) -> str:
    """Quote data for use in a URL path or query using UTF-8.

    Basic wrapper around :func:`urllib.parse.quote` when given a
    string, or :func:`urllib.parse.urlencode` for a dict or iterable.

    :param value: Data to quote. A string will be quoted directly. A
        dict or iterable of ``(key, value)`` pairs will be joined as a
        query string.
    :param encoding: The encoding to use for quoted strings.
    :param for_qs: If ``True``, quote ``/`` as ``%2F``. If ``False``,
        leave slashes unquoted. Defaults to ``False``.

    When given a string, "/" is not quoted. HTTP servers treat "/" and
    "%2F" equivalently in paths. If you need quoted slashes, use the
    ``|replace("/", "%2F")`` filter.

    .. versionadded:: 2.7
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        return url_quote(value, charset=encoding, for_qs=for_qs)

    if isinstance(value, dict):
        items: Iterable[Tuple[str, Any]] = value.items()
    else:
        items = value  # type: ignore

    return "&".join(
        f"{url_quote(k, for_qs=True)}={url_quote(v, for_qs=True)}" for k, v in items
    )


def to_bool(value):
    ''' return a bool for the arg '''
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.lower()
    if value in ('yes', 'on', '1', 'true', 1):
        return True
    return False


def quote_chinese(value, sep="", encoding="utf-8", decoding="utf-8"):
    if isinstance(value, str):
        return quote_chinese(value.encode(encoding))
    if isinstance(value, bytes):
        value = value.decode(decoding)
    res = [b if ord(b) < 128 else urllib_parse.quote(b) for b in value]
    if sep is not None:
        return sep.join(res)
    return res


# ---------------------------------------------------------------------------
# Content decoding helpers
# ---------------------------------------------------------------------------

def get_encodings_from_content(content):
    """Returns encodings from given content.

    :param content: bytestring or str to extract encodings from.
    """
    # 修复: 旧实现用 str 正则匹配 bytes 会抛 TypeError -> 在 find_encoding 里被 except 吞掉并恒
    # 回退 utf-8, 页面内 <meta charset>/XML 声明的编码从未被真正读取。对 bytes 先取一个可匹配的
    # latin-1 文本视图(仅用于扫 charset 标记, 不影响最终按探测到的编码解码)。
    if isinstance(content, (bytes, bytearray)):
        text = bytes(content).decode("latin-1", "ignore")
    else:
        text = content

    charset_re = re.compile(r'<meta.*?charset=["\']*(.+?)["\'>]', flags=re.I)
    pragma_re = re.compile(r'<meta.*?content=["\']*;?charset=(.+?)["\'>]', flags=re.I)
    xml_re = re.compile(r'^<\?xml.*?encoding=["\']*(.+?)["\'>]')

    return (
        charset_re.findall(text)
        + pragma_re.findall(text)
        + xml_re.findall(text)
    )


def _valid_codec(name):
    """名字是否为可用的 Python codec(供 decode 前过滤非法/私有 charset 名)。"""
    if not name:
        return False
    try:
        codecs.lookup(name)
        return True
    except LookupError:
        return False


def find_encoding(content, headers=None):
    # content is unicode
    if isinstance(content, str):
        return 'utf-8'

    encoding = None

    # 1) Content-Type 头里的 charset
    if headers:
        encoding = get_encoding_from_headers(headers)
        if encoding == 'ISO-8859-1':
            encoding = None
    # 头里的 charset 也可能是非法/私有名(如 x-gbk): 若非法则丢弃, 让位给下面的探测/页面内 <meta>,
    # 否则一个坏的 header charset 会挡住本可正确解码的页面, 令 decode 返回 None(Codex#5)。
    if encoding and not _valid_codec(encoding):
        encoding = None

    # 2) charset_normalizer 探测
    if not encoding and charset_normalizer is not None:
        encoding = charset_normalizer.detect(content)['encoding']

    # 3) 页面内 <meta>/XML 声明
    if not encoding:
        try:
            found = get_encodings_from_content(content)
            encoding = found[0] if found else None
        except Exception as e:
            logger_util.debug(e, exc_info=config.traceback_print)
            encoding = None

    if encoding and encoding.lower() == 'gb2312':
        encoding = 'gb18030'

    # 统一兜底校验: 任何来源(头/探测/页面)得到的编码名都必须是合法 codec, 否则 decode 会返回 None。
    if not _valid_codec(encoding):
        encoding = None

    if encoding:
        return encoding
    # bytes 无法确定编码时默认回退 utf-8(与本次改动前一致, 避免退成 latin_1 把 UTF-8 多字节读成乱码);
    # str 分支已在函数首行返回, 这里的 latin_1 仅为极端非 bytes 情况兜底。
    return 'utf-8' if isinstance(content, (bytes, bytearray)) else 'latin_1'


def decode(content, headers=None):
    encoding = find_encoding(content, headers)
    if encoding == 'unicode':
        return content

    try:
        return content.decode(encoding, 'replace')
    except Exception as e:
        logger_util.error('utils.decode: %s', e, exc_info=config.traceback_print)
        return None


# ---------------------------------------------------------------------------
# Arithmetic / math helpers
# ---------------------------------------------------------------------------

def is_num(value: str = ''):
    value = str(value)
    if value.count('.') == 1:
        tmp = value.split('.')
        return tmp[0].lstrip('-').isdigit() and tmp[1].isdigit()
    else:
        return value.lstrip('-').isdigit()


def add(*args):
    result = 0
    if args and is_num(args[0]):
        result = float(args[0])
        for i in args[1:]:
            if is_num(i):
                result += float(i)
            else:
                return
        return f"{result:f}"
    return result


def sub(*args):
    result = 0
    if args and is_num(args[0]):
        result = float(args[0])
        for i in args[1:]:
            if is_num(i):
                result -= float(i)
            else:
                return
        return f"{result:f}"
    return result


def multiply(*args):
    result = 0
    if args and is_num(args[0]):
        result = float(args[0])
        for i in args[1:]:
            if is_num(i):
                result *= float(i)
            else:
                return
        return f"{result:f}"
    return result


def divide(*args):
    result = 0
    if args and is_num(args[0]):
        result = float(args[0])
        for i in args[1:]:
            if is_num(i) and float(i) != 0:
                result /= float(i)
            else:
                return
        return f"{result:f}"
    return result


# ---------------------------------------------------------------------------
# Random helpers
# ---------------------------------------------------------------------------

def get_random(min_num, max_num, unit):
    random_num = random.uniform(min_num, max_num)
    result = f"{random_num:.{int(unit)}f}"
    return result


def random_fliter(*args, **kwargs):
    try:
        result = get_random(*args, **kwargs)
    except Exception as e:
        logger_util.debug(e, exc_info=config.traceback_print)
        result = random.choice(*args, **kwargs)
    return result


def randomize_list(mylist, seed=None):
    try:
        mylist = list(mylist)
        if seed:
            r = random.Random(seed)
            r.shuffle(mylist)
        else:
            random.shuffle(mylist)
    except Exception as e:
        logger_util.debug(e, exc_info=config.traceback_print)
        raise e
    return mylist


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

def regex_replace(value='', pattern='', replacement='', count=0, ignorecase=False, multiline=False):
    ''' Perform a `re.sub` returning a string '''

    value = to_text(value, errors='surrogate_or_strict', nonstring='simplerepr')

    flags = 0
    if ignorecase:
        flags |= re.I
    if multiline:
        flags |= re.M
    _re = re.compile(pattern, flags=flags)
    return _re.sub(replacement, value, count)


def regex_findall(value, pattern, ignorecase=False, multiline=False):
    ''' Perform re.findall and return the list of matches '''

    value = to_text(value, errors='surrogate_or_strict', nonstring='simplerepr')

    flags = 0
    if ignorecase:
        flags |= re.I
    if multiline:
        flags |= re.M
    return str(re.findall(pattern, value, flags))


def regex_search(value, pattern, *args, **kwargs):
    ''' Perform re.search and return the list of matches or a backref '''

    value = to_text(value, errors='surrogate_or_strict', nonstring='simplerepr')

    groups = list()
    for arg in args:
        if arg.startswith('\\g'):
            match = re.match(r'\\g<(\S+)>', arg).group(1)
            groups.append(match)
        elif arg.startswith('\\'):
            match = int(re.match(r'\\(\d+)', arg).group(1))
            groups.append(match)
        else:
            raise Exception('Unknown argument')

    flags = 0
    if kwargs.get('ignorecase'):
        flags |= re.I
    if kwargs.get('multiline'):
        flags |= re.M

    match = re.search(pattern, value, flags)
    if match:
        if not groups:
            return str(match.group())
        else:
            items = list()
            for item in groups:
                items.append(match.group(item))
            return str(items)


def regex_escape(value, re_type='python'):
    value = to_text(value, errors='surrogate_or_strict', nonstring='simplerepr')
    # '''Escape all regular expressions special characters from STRING.'''
    if re_type == 'python':
        return re.escape(value)
    if re_type == 'posix_basic':
        # list of BRE special chars:
        # https://en.wikibooks.org/wiki/Regular_Expressions/POSIX_Basic_Regular_Expressions
        return regex_replace(value, r'([].[^$*\\])', r'\\\1')
    # TODO: implement posix_extended
    # It's similar to, but different from python regex, which is similar to,
    # but different from PCRE.  It's possible that re.escape would work here.
    # https://remram44.github.io/regex-cheatsheet/regex.html#programs
    elif re_type == 'posix_extended':
        raise Exception(f'Regex type ({re_type}) not yet implemented')
    else:
        raise Exception(f'Invalid regex type ({re_type})')


# ---------------------------------------------------------------------------
# Misc Jinja helpers
# ---------------------------------------------------------------------------

def ternary(value, true_val, false_val, none_val=None):
    '''  value ? true_val : false_val '''
    if (value is None or isinstance(value, Undefined)) and none_val is not None:
        return none_val
    elif bool(value):
        return true_val
    else:
        return false_val


def mandatory(value, msg=None):
    ''' Make a variable mandatory '''
    if isinstance(value, Undefined):
        # pylint: disable=protected-access
        if value._undefined_name is not None:
            name = f"'{to_text(value._undefined_name)}' "
        else:
            name = ''

        if msg is not None:
            raise Exception(to_native(msg))
        else:
            raise Exception(f"Mandatory variable {name} not defined.")

    return value


# ---------------------------------------------------------------------------
# Jinja globals dict
# ---------------------------------------------------------------------------

from binascii import (a2b_base64, a2b_hex, a2b_qp, a2b_uu,  # noqa: E402
                      b2a_base64, b2a_hex, b2a_qp, b2a_uu, crc32, crc_hqx)

jinja_globals = {
    # types
    'int': do_int,
    'float': do_float,
    'bool': to_bool,
    'utf8': utf8,
    'unicode': conver2unicode,
    'urlencode': urlencode_with_encoding,
    'quote_chinese': quote_chinese,
    # binascii
    'b2a_hex': b2a_hex,
    'a2b_hex': a2b_hex,
    'b2a_uu': b2a_uu,
    'a2b_uu': a2b_uu,
    'b2a_base64': b2a_base64,
    'a2b_base64': a2b_base64,
    'b2a_qp': b2a_qp,
    'a2b_qp': a2b_qp,
    'crc_hqx': crc_hqx,
    'crc32': crc32,
    # format
    'format': format,
    # base64
    'b64decode': b64decode,
    'b64encode': b64encode,
    # uuid
    'to_uuid': to_uuid,
    # hash filters
    # md5 hex digest of string
    'md5': md5string,
    # sha1 hex digest of string
    'sha1': secure_hash_s,
    # generic hashing
    'password_hash': get_encrypted_password,
    'hash': get_hash,
    'aes_encrypt': _aes_encrypt,
    'aes_decrypt': _aes_decrypt,
    # time
    'timestamp': timestamp,
    'date_time': get_date_time,
    # Calculate
    'is_num': is_num,
    'add': add,
    'sub': sub,
    'multiply': multiply,
    'divide': divide,
    'Faker': Faker,
    # regex
    'regex_replace': regex_replace,
    'regex_escape': regex_escape,
    'regex_search': regex_search,
    'regex_findall': regex_findall,
    # ? : ;
    'ternary': ternary,
    # random stuff
    'random': random_fliter,
    'shuffle': randomize_list,
    # undefined
    'mandatory': mandatory,
    # debug
    'type_debug': lambda value: value.__class__.__name__,
}

jinja_inner_globals = {
    'dict': dict,
    'lipsum': generate_lorem_ipsum,
    'range': range,
}
