#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Author: Binux<i@binux.me>
#         http://binux.me
# Created on 2014-08-07 22:00:27
#
# Thin re-export shim.  All implementations now live in libs/_utils/.
# This file keeps every original public name importable from libs.utils
# so that all existing callers continue to work without modification.

# -- network / IP ----------------------------------------------------------
from libs._utils.network import (
    ip2int,
    ip2varbinary,
    is_lan,
    int2ip,
    varbinary2ip,
    is_ip,
    urlmatch,
    url_match_with_limit,
    domain_match,
)

# -- crypto / hashing / encoding -------------------------------------------
from libs._utils.crypto import (
    secure_hash_s,
    md5string,
    get_hash,
    get_encrypted_password,
    b64encode,
    b64decode,
    to_uuid,
    switch_mode,
    _aes_encrypt,
    _aes_decrypt,
    # binascii re-exports
    a2b_base64,
    a2b_hex,
    a2b_qp,
    a2b_uu,
    b2a_base64,
    b2a_hex,
    b2a_qp,
    b2a_uu,
    crc32,
    crc_hqx,
)

# -- date / time -----------------------------------------------------------
from libs._utils.datetime_fmt import (
    format_date,
    get_date_time,
    strftime,
    timestamp,
)

# -- mail ------------------------------------------------------------------
from libs._utils.mail import (
    send_mail,
    _send_mail,
)

# -- jinja filters / globals and their helper functions --------------------
from libs._utils.jinja_filters import (
    utf8,
    conver2unicode,
    urlencode_with_encoding,
    to_bool,
    quote_chinese,
    get_encodings_from_content,
    find_encoding,
    decode,
    is_num,
    add,
    sub,
    multiply,
    divide,
    get_random,
    random_fliter,
    randomize_list,
    regex_replace,
    regex_findall,
    regex_search,
    regex_escape,
    ternary,
    mandatory,
    jinja_globals,
    jinja_inner_globals,
)

# -- misc (func_cache / method_cache) -------------------------------------
from libs._utils.misc import (
    func_cache,
    method_cache,
)
