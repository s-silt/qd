#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Miscellaneous helpers extracted from libs/utils.py
# (func_cache / method_cache and content-decoding utilities)
# pylint: disable=broad-exception-raised

import functools

import umsgpack  # type: ignore


def func_cache(f):
    _cache = {}

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        key = umsgpack.packb((args, kwargs))
        if key not in _cache:
            _cache[key] = f(*args, **kwargs)
        return _cache[key]

    return wrapper


def method_cache(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        # pylint: disable=protected-access
        tmp = {}
        for k, v in kwargs.items():
            if k == 'sql_session':
                continue
            tmp[k] = v
        if not hasattr(self, '_cache'):
            self._cache = {}
        key = umsgpack.packb((args, tmp))
        if key not in self._cache:
            self._cache[key] = fn(self, *args, **kwargs)
        return self._cache[key]

    return wrapper
