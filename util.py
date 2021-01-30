# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

import collections
import errno
import io
import os
import socket
import sys
import threading
import time
import warnings

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any, Deque, Dict, Generic, List, Optional, Tuple, TypeVar

try:
    import queue
except ImportError:
    import Queue as queue  # type: ignore[no-redef]

try:
    from http.cookiejar import MozillaCookieJar
except ImportError:
    from cookielib import MozillaCookieJar  # type: ignore[no-redef]

_PATH_IS_ON_VFAT_WORKS = True

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[no-redef]
    _PATH_IS_ON_VFAT_WORKS = False

if os.name == 'nt':
    try:
        from nt import _getvolumepathname  # type: ignore[no-redef]
    except ImportError:
        _getvolumepathname = None  # type: ignore[no-redef]
        _PATH_IS_ON_VFAT_WORKS = False

try:
    from urllib3.exceptions import DependencyWarning
    URLLIB3_FROM_PIP = False
except ImportError:
    try:
        # pip includes urllib3
        from pip._vendor.urllib3.exceptions import DependencyWarning
        URLLIB3_FROM_PIP = True
    except ImportError:
        raise RuntimeError('The urllib3 module is required. Please install it with pip or your package manager.')

# This builtin has a new name in Python 3
try:
    raw_input  # type: ignore[has-type]
except NameError:
    raw_input = input

PY3 = sys.version_info[0] >= 3

try:
    from ssl import HAS_SNI as SSL_HAS_SNI
except ImportError:
    SSL_HAS_SNI = False


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


class ConnectionFile(object):
    def __init__(self, conn, *args, **kwargs):
        kwargs.setdefault('closefd', False)
        self.conn = conn
        self.file = io.open(conn.fileno(), *args, **kwargs)

    def __enter__(self):
        return self.file.__enter__()

    def __exit__(self, *excinfo):
        self.file.__exit__(*excinfo)
        self.conn.close()


# contextlib.nullcontext, not available in Python 2
class nullcontext(object):
    def __enter__(self):
        return None

    def __exit__(self, *excinfo):
        pass


KNOWN_GOOD_NAMESERVER = '8.8.8.8'
# DNS query for 'A' record of 'google.com'.
# Generated using python -c "import dnslib; print(bytes(dnslib.DNSRecord.question('google.com').pack()))"
DNS_QUERY = b'\xf1\xe1\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01'


def is_dns_working(timeout=None):
    sock = None
    try:
        # Would use a with statement, but that doesn't work on Python 2, mumble mumble
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if timeout is not None:
            sock.settimeout(timeout)
        sock.sendto(DNS_QUERY, (KNOWN_GOOD_NAMESERVER, 53))
        sock.recvfrom(1)
    except EnvironmentError:
        return False
    finally:
        if sock is not None:
            sock.close()

    return True


def rstrip_slashes(path):
    return path.rstrip(b'\\/' if isinstance(path, bytes) else u'\\/')


class _Path_Is_On_VFat(object):
    works = _PATH_IS_ON_VFAT_WORKS

    def __call__(self, path):
        if not self.works:
            raise RuntimeError('This function must not be called unless PATH_IS_ON_VFAT_WORKS is True')

        if os.name == 'nt':
            # Compare normalized absolute path of volume
            getdev = rstrip_slashes
            path_dev = rstrip_slashes(_getvolumepathname(path))
        else:
            # Compare device ID
            def getdev(mount): return os.stat(mount).st_dev
            path_dev = getdev(path)

        return any(part.fstype == 'vfat' and getdev(part.mountpoint) == path_dev
                   for part in psutil.disk_partitions(all=True))


path_is_on_vfat = _Path_Is_On_VFat()


class WaitOnMainThread(object):
    def __init__(self):
        self.cond = None  # type: Optional[threading.Condition]
        self.flag = False  # type: Optional[bool]

    def setup(self, lock=None):
        self.cond = threading.Condition(lock)

    def signal(self):
        assert self.cond is not None
        if isinstance(threading.current_thread(), threading._MainThread):  # type: ignore[attr-defined]
            self._do_wait()
            return

        with self.cond:
            if self.flag is None:
                sys.exit(1)
            self.flag = True
            self.cond.wait()
            if self.flag is None:
                sys.exit(1)

    # Call on main thread when signaled or idle. If the lock is held, pass release=True.
    def check(self, release=False):
        assert self.cond is not None
        if self.flag is False:
            return

        if release:
            saved_state = lock_release_save(self.cond)
            try:
                self._do_wait()
            finally:
                lock_acquire_restore(self.cond, saved_state)
        else:
            self._do_wait()

        with self.cond:
            self.flag = False
            self.cond.notify_all()

    # Call on main thread to prevent threads from blocking in signal()
    def destroy(self):
        assert self.cond is not None
        if self.flag is None:
            return

        with self.cond:
            self.flag = None  # Cause all waiters to exit
            self.cond.notify_all()

    def _do_wait(self):
        assert self.cond is not None
        if self.flag is None:
            raise RuntimeError('Broken WaitOnMainThread cannot be reused')

        try:
            self._wait()
        except:
            with self.cond:
                self.flag = None  # Waiting never completed
                self.cond.notify_all()
            raise

    @staticmethod
    def _wait():
        raise NotImplementedError


class NoInternet(WaitOnMainThread):
    @staticmethod
    def _wait():
        # Having no internet is a temporary system error
        # Wait 30 seconds at first, then exponential backoff up to 15 minutes
        print('DNS probe finished: No internet. Waiting...', file=sys.stderr)
        sleep_time = 30
        while True:
            time.sleep(sleep_time)
            if is_dns_working():
                break
            sleep_time = min(sleep_time * 2, 900)


no_internet = NoInternet()


# Set up ssl for urllib3. This should be called before using urllib3 or importing requests.
def setup_urllib3_ssl():
    # Don't complain about missing SOCKS dependencies
    warnings.filterwarnings('ignore', category=DependencyWarning)

    try:
        import ssl
    except ImportError:
        return

    # Inject SecureTransport on macOS if the linked OpenSSL is too old to handle TLSv1.2
    if sys.platform == 'darwin' and ssl.OPENSSL_VERSION_NUMBER < 0x1000100F:
        try:
            if URLLIB3_FROM_PIP:
                from pip._vendor.urllib3.contrib import securetransport
            else:
                from urllib3.contrib import securetransport
        except (ImportError, EnvironmentError):
            pass
        else:
            securetransport.inject_into_urllib3()

    # Inject PyOpenSSL if the linked OpenSSL has no SNI
    if not SSL_HAS_SNI:
        try:
            if URLLIB3_FROM_PIP:
                from pip._vendor.urllib3.contrib import pyopenssl
            else:
                from urllib3.contrib import pyopenssl
        except ImportError:
            pass
        else:
            pyopenssl.inject_into_urllib3()


def get_supported_encodings():
    encodings = ['deflate', 'gzip']
    try:
        from brotli import brotli
    except ImportError:
        pass
    else:
        encodings.insert(0, 'br')  # brotli takes priority if available
    return encodings


def make_requests_session(session_type, retry, timeout, verify, user_agent, cookiefile):
    class SessionWithTimeout(session_type):  # type: ignore[misc,valid-type]
        def request(self, method, url, **kwargs):
            kwargs.setdefault('timeout', timeout)
            return super(SessionWithTimeout, self).request(method, url, **kwargs)

    session = SessionWithTimeout()
    session.verify = verify
    session.headers['Accept-Encoding'] = ', '.join(get_supported_encodings())
    if user_agent is not None:
        session.headers['User-Agent'] = user_agent
    for adapter in session.adapters.values():
        adapter.max_retries = retry
    if cookiefile is not None:
        cookies = MozillaCookieJar(cookiefile)
        cookies.load()

        # Session cookies are denoted by either `expires` field set to an empty string or 0. MozillaCookieJar only
        # recognizes the former (see https://bugs.python.org/issue17164).
        for cookie in cookies:
            if cookie.expires == 0:
                cookie.expires = None
                cookie.discard = True

        session.cookies = cookies  # type: ignore[assignment]
    return session


if TYPE_CHECKING:
    if PY3:
        WaiterSeq = Deque[Any]
    else:
        WaiterSeq = List[Any]
    MCBase = threading.Condition
elif PY3:
    WaiterSeq = collections.deque
    MCBase = threading.Condition
else:
    WaiterSeq = list
    MCBase = threading._Condition


# Minimal implementation of a sum of mutable sequences
class MultiSeqProxy(object):
    def __init__(self, subseqs):
        self.subseqs = subseqs

    def append(self, value):
        for sub in self.subseqs:
            sub.append((value, self.subseqs))

    def remove(self, value):
        for sub in self.subseqs:
            sub.remove((value, self.subseqs))


# Hooks into methods used by threading.Condition.notify
class NotifierWaiters(WaiterSeq):
    def __iter__(self):
        return (value[0] for value in super(NotifierWaiters, self).__iter__())

    def __getitem__(self, index):
        item = super(NotifierWaiters, self).__getitem__(index)
        return WaiterSeq(v[0] for v in item) if isinstance(index, slice) else item[0]  # pytype: disable=not-callable

    if not PY3:
        def __getslice__(self, i, j):
            return self[max(0, i):max(0, j):]

    def remove(self, value):
        try:
            match = next(x for x in super(NotifierWaiters, self).__iter__() if x[0] == value)
        except StopIteration:
            raise ValueError('deque.remove(x): x not in deque')
        for ref in match[1]:
            try:
                super(NotifierWaiters, ref).remove(match)  # Remove waiter from known location
            except ValueError:
                raise RuntimeError('Unexpected missing waiter!')


# Supports waiting on multiple threading.Conditions objects simultaneously
class MultiCondition(MCBase):
    def __init__(self, lock):
        super(MultiCondition, self).__init__(lock)

    def wait(self, children, timeout=None):
        def get_waiters(c):    return getattr(c, '_waiters' if PY3 else '_Condition__waiters')
        def set_waiters(c, v):        setattr(c, '_waiters' if PY3 else '_Condition__waiters', v)
        def get_lock(c):       return getattr(c, '_lock'    if PY3 else '_Condition__lock')

        assert len(frozenset(id(c) for c in children)) == len(children), 'Children must be unique'
        assert all(get_lock(c) is get_lock(self) for c in children), 'All locks must be the same'

        # Modify children so their notify methods do cleanup
        for child in children:
            if not isinstance(get_waiters(child), NotifierWaiters):
                set_waiters(child, NotifierWaiters((w, (get_waiters(child),))
                                                   for w in get_waiters(child)))
        set_waiters(self, MultiSeqProxy(tuple(get_waiters(c) for c in children)))

        super(MultiCondition, self).wait(timeout)

    def notify(self, n=1):
        raise NotImplementedError

    def notify_all(self):
        raise NotImplementedError

    notifyAll = notify_all


def lock_is_owned(lock):
    try:
        return lock._is_owned()
    except AttributeError:
        if lock.acquire(0):
            lock.release()
            return False
        return True


def lock_release_save(lock):
    try:
        return lock._release_save()  # pytype: disable=attribute-error
    except AttributeError:
        lock.release()  # No state to save
        return None


def lock_acquire_restore(lock, state):
    try:
        lock._acquire_restore(state)  # pytype: disable=attribute-error
    except AttributeError:
        lock.acquire()  # Ignore saved state


class AsyncCallable(object):
    def __init__(self, lock, fun, name=None):
        self.lock = lock
        self.fun = fun
        if TYPE_CHECKING:
            Params = Tuple[Tuple[Any, ...], Dict[str, Any]]  # (args, kwargs)
        self.request = LockedQueue(lock, maxsize=1)  # type: LockedQueue[Optional[Params]]
        self.response = LockedQueue(lock, maxsize=1)  # type: LockedQueue[Any]
        self.quit_flag = False
        if PY3:
            self.thread = threading.Thread(target=self.run_thread, name=name, daemon=True)
        else:
            self.thread = threading.Thread(target=self.run_thread, name=name)
        self.thread.start()

    def run_thread(self):
        while not self.quit_flag:
            request = self.request.get()
            if request is None:
                break  # quit sentinel
            args, kwargs = request
            response = self.fun(*args, **kwargs)
            self.response.put(response)

    def put(self, *args, **kwargs):
        self.request.put((args, kwargs))

    def get(self, *args, **kwargs):
        return self.response.get(*args, **kwargs)

    def quit(self):
        self.quit_flag = True
        # Make sure the thread wakes up
        try:
            self.request.put(None, block=False)
        except queue.Full:
            pass
        self.thread.join()


def opendir(dir_, flags):
    try:
        flags |= os.O_DIRECTORY
    except AttributeError:
        dir_ += os.path.sep  # Fallback, some systems don't support O_DIRECTORY
    return os.open(dir_, flags)


def try_unlink(path):
    try:
        os.unlink(path)
    except EnvironmentError as e:
        if getattr(e, 'errno', None) != errno.ENOENT:
            raise
