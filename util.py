# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

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
    from typing import Generic, Optional, TypeVar

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

    # Call on main thread when signaled or idle.
    def check(self):
        assert self.cond is not None
        if self.flag is False:
            return

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
