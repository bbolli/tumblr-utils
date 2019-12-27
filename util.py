# -*- coding: utf-8 -*-

import sys

PY3 = sys.version_info[0] >= 3


def to_unicode(string, encoding='utf-8', errors='strict'):
    if isinstance(string, bytes):
        return string.decode(encoding, errors)
    return string


def to_bytes(string, encoding='utf-8', errors='strict'):
    if isinstance(string, bytes):
        return string
    return string.encode(encoding, errors)


def to_native_str(string, encoding='utf-8', errors='strict'):
    if PY3:
        return to_unicode(string, encoding, errors)
    else:
        return to_bytes(string, encoding, errors)
