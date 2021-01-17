# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

import sys
import threading

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Generic, TypeVar

try:
    import queue
except ImportError:
    import Queue as queue  # type: ignore[no-redef]

PY3 = sys.version_info[0] >= 3
HAVE_SSL_CTX = sys.version_info >= (2, 7, 9)

HTTP_TIMEOUT = 90


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


if TYPE_CHECKING:
    T = TypeVar('T')

    class GenericQueue(queue.Queue[T], Generic[T]):
        pass
else:
    T = None

    class FakeGenericMeta(type):
        def __getitem__(cls, item):
            return cls

    if PY3:
        exec("""class GenericQueue(queue.Queue, metaclass=FakeGenericMeta):
    pass""")
    else:
        class GenericQueue(queue.Queue, object):
            __metaclass__ = FakeGenericMeta


class LockedQueue(GenericQueue[T]):
    def __init__(self, lock, maxsize=0):
        super(LockedQueue, self).__init__(maxsize)
        self.mutex = lock
        self.not_empty = threading.Condition(lock)
        self.not_full = threading.Condition(lock)
        self.all_tasks_done = threading.Condition(lock)
