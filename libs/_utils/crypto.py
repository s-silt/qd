#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Crypto / encoding / hashing helpers extracted from libs/utils.py
# pylint: disable=broad-exception-raised

import base64
import hashlib
import uuid
from binascii import (a2b_base64, a2b_hex, a2b_qp, a2b_uu, b2a_base64, b2a_hex,
                      b2a_qp, b2a_uu, crc32, crc_hqx)
from hashlib import sha1
from typing import Union

from Crypto.Cipher import AES

from libs.convert import to_bytes, to_native, to_text
from libs.mcrypto import aes_decrypt, aes_encrypt, passlib_or_crypt

try:
    from hashlib import md5 as _md5  # pylint: disable=ungrouped-imports
except ImportError:
    # Assume we're running in FIPS mode here
    _md5 = None  # type: ignore

# Re-export binascii names so callers can grab them from crypto too
__all__ = [
    'a2b_base64', 'a2b_hex', 'a2b_qp', 'a2b_uu',
    'b2a_base64', 'b2a_hex', 'b2a_qp', 'b2a_uu',
    'crc32', 'crc_hqx',
    'secure_hash_s', 'md5string', 'get_hash', 'get_encrypted_password',
    'b64encode', 'b64decode',
    'to_uuid',
    'switch_mode', '_aes_encrypt', '_aes_decrypt',
]


def secure_hash_s(value, hash_func=sha1):
    ''' Return a secure hash hex digest of data. '''

    digest = hash_func()
    value = to_bytes(value, errors='surrogate_or_strict')
    digest.update(value)
    return digest.hexdigest()


def md5string(value):
    if _md5 is None:
        raise ValueError('MD5 not available. Possibly running in FIPS mode')
    return secure_hash_s(value, _md5)


def get_hash(value, hashtype='sha1'):
    try:
        h = hashlib.new(hashtype)
    except Exception as e:
        # hash is not supported?
        raise e

    h.update(to_bytes(value, errors='surrogate_or_strict'))
    return h.hexdigest()


def get_encrypted_password(password, hashtype='sha512', salt=None, salt_size=None, rounds=None, ident=None):
    passlib_mapping = {
        'md5': 'md5_crypt',
        'blowfish': 'bcrypt',
        'sha256': 'sha256_crypt',
        'sha512': 'sha512_crypt',
    }

    hashtype = passlib_mapping.get(hashtype, hashtype)
    return passlib_or_crypt(password, hashtype, salt=salt, salt_size=salt_size, rounds=rounds, ident=ident)


def b64encode(value, encoding='utf-8'):
    return to_text(base64.b64encode(to_bytes(value, encoding=encoding, errors='surrogate_or_strict')))


def b64decode(value, encoding='utf-8'):
    return to_text(base64.b64decode(to_bytes(value, errors='surrogate_or_strict')), encoding=encoding)


def to_uuid(value, namespace=uuid.NAMESPACE_URL):
    uuid_namespace = namespace
    if not isinstance(uuid_namespace, uuid.UUID):
        try:
            uuid_namespace = uuid.UUID(namespace)
        except (AttributeError, ValueError) as e:
            raise Exception(f"Invalid value '{to_native(namespace)}' for 'namespace': {to_native(e)}") from e
    # uuid.uuid5() requires bytes on Python 2 and bytes or text or Python 3
    return to_text(uuid.uuid5(uuid_namespace, to_native(value, errors='surrogate_or_strict')))


def switch_mode(mode):
    mode = mode.upper()
    if mode == 'CBC':
        return AES.MODE_CBC
    elif mode == 'ECB':
        return AES.MODE_ECB
    elif mode == 'CFB':
        return AES.MODE_CFB
    elif mode == 'OFB':
        return AES.MODE_OFB
    elif mode == 'CTR':
        return AES.MODE_CTR
    elif mode == 'OPENPGP':
        return AES.MODE_OPENPGP
    elif mode == 'GCM':
        return AES.MODE_GCM
    elif mode == 'CCM':
        return AES.MODE_CCM
    elif mode == 'SIV':
        return AES.MODE_SIV
    elif mode == 'OCB':
        return AES.MODE_OCB
    elif mode == 'EAX':
        return AES.MODE_EAX
    else:
        raise Exception(f'Invalid AES mode: {mode}')


def _aes_encrypt(word: str, key: str, mode='CBC', iv: Union[str, bytes, None] = None, output_format='base64', padding=True, padding_style='pkcs7', no_packb=True):
    if key is None:
        raise Exception('key is required')
    if isinstance(iv, str):
        iv = iv.encode("utf-8")
    mode = switch_mode(mode)
    return aes_encrypt(word.encode("utf-8"), key.encode("utf-8"), mode=mode, iv=iv, output=output_format, padding=padding, padding_style=padding_style, no_packb=no_packb)


def _aes_decrypt(word: str, key: str, mode='CBC', iv: Union[str, bytes, None] = None, input_format='base64', padding=True, padding_style='pkcs7', no_packb=True):
    if key is None:
        raise Exception('key is required')
    if isinstance(iv, str):
        iv = iv.encode("utf-8")
    mode = switch_mode(mode)
    return aes_decrypt(word.encode("utf-8"), key.encode("utf-8"), mode=mode, iv=iv, input=input_format, padding=padding, padding_style=padding_style, no_packb=no_packb)
