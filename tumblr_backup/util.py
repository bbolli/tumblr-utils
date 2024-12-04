from __future__ import annotations

import errno
import os
import queue
import shutil
import socket
import sys
import threading
import time
import warnings
from abc import ABC, abstractmethod
from collections import deque
from enum import Enum
from functools import total_ordering
from http.cookiejar import MozillaCookieJar
from importlib.machinery import PathFinder
from typing import TYPE_CHECKING, Any, Deque, Generic, TypeVar

from requests.adapters import HTTPAdapter
from urllib3.exceptions import DependencyWarning

if sys.platform == 'darwin':
    import fcntl

if TYPE_CHECKING:
    import requests
    from typing_extensions import TypeAlias
    swt_base = requests.Session


def to_bytes(string, encoding='utf-8', errors='strict'):
    if isinstance(string, bytes):
        return string
    return string.encode(encoding, errors)


class FakeGenericMeta(type):
    def __getitem__(cls, item):
        return cls


if TYPE_CHECKING:
    T = TypeVar('T')

    class GenericQueue(queue.Queue[T], Generic[T]):
        pass
else:
    T = None

    class GenericQueue(queue.Queue, metaclass=FakeGenericMeta):
        pass


class LockedQueue(GenericQueue[T]):
    def __init__(self, lock, maxsize=0):
        super().__init__(maxsize)
        self.mutex = lock
        self.not_empty = threading.Condition(lock)
        self.not_full = threading.Condition(lock)
        self.all_tasks_done = threading.Condition(lock)


class ConnectionFile:
    def __init__(self, conn, *args, **kwargs):
        kwargs.setdefault('closefd', False)
        self.conn = conn
        self.file = open(conn.fileno(), *args, **kwargs)

    def __enter__(self):
        return self.file.__enter__()

    def __exit__(self, *excinfo):
        self.file.__exit__(*excinfo)
        self.conn.close()


KNOWN_GOOD_NAMESERVER = '8.8.8.8'
# DNS query for 'A' record of 'google.com'.
# Generated using python -c "import dnslib; print(bytes(dnslib.DNSRecord.question('google.com').pack()))"
DNS_QUERY = b'\xf1\xe1\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01'


def is_dns_working(timeout=None, check=True):
    if not check:
        return True  # assume internet is OK

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if timeout is not None:
                sock.settimeout(timeout)
            sock.sendto(DNS_QUERY, (KNOWN_GOOD_NAMESERVER, 53))
            sock.recvfrom(1)
    except OSError:
        return False

    return True


class WaitOnMainThread(ABC):
    def __init__(self):
        self.cond: threading.Condition | None = None
        self.flag: bool | None = False

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
    @abstractmethod
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


class Enospc(WaitOnMainThread):
    @staticmethod
    def _wait():
        if not os.isatty(sys.stdin.fileno()):
            # Pausing or consuming input does no good during unattended execution.
            # We have no hope of recovering, so raise an uncaught exception.
            raise RuntimeError(OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)))
        print('Error: No space left on device. Press Enter to try again...', file=sys.stderr)
        input()


no_internet = NoInternet()
enospc = Enospc()


# Set up ssl for urllib3. This should be called before using urllib3 or importing requests.
def setup_urllib3_ssl():
    # Don't complain about missing SOCKS dependencies
    warnings.filterwarnings('ignore', category=DependencyWarning)

    try:
        import ssl
    except ImportError:
        return  # Can't do anything without this module

    have_sni = getattr(ssl, 'HAS_SNI', False)

    # Inject SecureTransport on macOS if the linked OpenSSL is too old to handle TLSv1.2 or doesn't support SNI
    if sys.platform == 'darwin' and (ssl.OPENSSL_VERSION_NUMBER < 0x1000100F or not have_sni):
        try:
            from urllib3.contrib import securetransport
        except (ImportError, OSError) as e:
            print('Warning: Failed to inject SecureTransport: {!r}'.format(e), file=sys.stderr)
        else:
            securetransport.inject_into_urllib3()
            have_sni = True  # SNI always works

    # Inject PyOpenSSL if the linked OpenSSL has no SNI
    if not have_sni:
        try:
            from urllib3.contrib import pyopenssl
            pyopenssl.inject_into_urllib3()
        except ImportError as e:
            print('Warning: Failed to inject pyOpenSSL: {!r}'.format(e), file=sys.stderr)
        else:
            have_sni = True  # SNI always works


def make_requests_session(session_type, retry, timeout, verify, user_agent, cookiefile):
    if TYPE_CHECKING:
        global swt_base
    else:
        swt_base = session_type  # type: ignore

    class SessionWithTimeout(swt_base):
        def request(self, method, url, *args, **kwargs):
            kwargs.setdefault('timeout', timeout)
            return super().request(method, url, *args, **kwargs)

    session = SessionWithTimeout()
    session.verify = verify
    if user_agent is not None:
        session.headers['User-Agent'] = user_agent
    for adapter in session.adapters.values():
        if isinstance(adapter, HTTPAdapter):
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


@total_ordering
class LogLevel(Enum):
    INFO = 0
    WARN = 1
    ERROR = 2

    def __lt__(self, other):
        if type(self) is type(other):
            return self.value < other.value
        return NotImplemented


def fsync(fd):
    if sys.platform == 'darwin':
        # Apple's fsync does not flush the drive write cache
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except OSError:
            pass  # fall back to fsync
        else:
            return
    os.fsync(fd)


def fdatasync(fd):
    if hasattr(os, 'fdatasync'):
        return os.fdatasync(fd)
    fsync(fd)


# Minimal implementation of a sum of mutable sequences
class MultiSeqProxy:
    def __init__(self, subseqs):
        self.subseqs = subseqs

    def append(self, value):
        for sub in self.subseqs:
            sub.append((value, self.subseqs))

    def remove(self, value):
        for sub in self.subseqs:
            sub.remove((value, self.subseqs))


# Hooks into methods used by threading.Condition.notify
class NotifierWaiters(Deque[Any]):
    def __iter__(self):
        return (value[0] for value in super(NotifierWaiters, self).__iter__())

    def __getitem__(self, index):
        item = super().__getitem__(index)
        return deque(v[0] for v in item) if isinstance(index, slice) else item[0]  # pytype: disable=not-callable

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
class MultiCondition(threading.Condition):
    def __init__(self, lock):  # noqa: WPS612
        super().__init__(lock)

    def wait(self, children, timeout=None):  # type: ignore[override] # pytype: disable=signature-mismatch
        assert len(frozenset(id(c) for c in children)) == len(children), 'Children must be unique'
        assert all(c._lock is self._lock for c in children), 'All locks must be the same'  # type: ignore[attr-defined]

        # Modify children so their notify methods do cleanup
        for child in children:
            if not isinstance(child._waiters, NotifierWaiters):
                child._waiters = NotifierWaiters(
                    ((w, (child._waiters,)) for w in child._waiters),
                )
        self._waiters = MultiSeqProxy(tuple(c._waiters for c in children))

        super().wait(timeout)

    def notify(self, n=1):
        raise NotImplementedError

    def notify_all(self):
        raise NotImplementedError

    notifyAll = notify_all  # noqa: N815


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


ACParams: TypeAlias = 'tuple[tuple[Any, ...], dict[str, Any]]'  # (args, kwargs)


class AsyncCallable:
    request: LockedQueue[ACParams | None]
    response: LockedQueue[Any]

    def __init__(self, lock, fun, name=None):
        self.lock = lock
        self.fun = fun
        self.request = LockedQueue(lock, maxsize=1)
        self.response = LockedQueue(lock, maxsize=1)
        self.quit_flag = False
        self.thread = threading.Thread(target=self.run_thread, name=name, daemon=True)
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
    except FileNotFoundError:
        pass  # ignored


def _copy_file_range(src, dst):
    if not hasattr(os, 'copy_file_range'):
        return False

    with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
        infd, outfd = fsrc.fileno(), fdst.fileno()
        blocksize = max(os.fstat(infd).st_size, 2 ** 23)  # min 8MiB
        if sys.maxsize < 2 ** 32:  # 32-bit architecture
            blocksize = min(blocksize, 2 ** 30)  # max 1GiB

        try:
            while True:
                bytes_copied = os.copy_file_range(infd, outfd, blocksize)  # type: ignore[attr-defined]
                if not bytes_copied:
                    return True  # EOF
        except OSError as e:
            if e.errno == errno.EXDEV:
                return False  # Different devices (pre Linux 5.3)
            e.filename, e.filename2 = src, dst
            raise e


def copyfile(src, dst):
    if _copy_file_range(src, dst):
        return dst
    return shutil.copyfile(src, dst)


def have_module(name):
    return PathFinder.find_spec(name) is not None
