# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

import functools
import io
import itertools
import os
import time
import warnings
from email.utils import mktime_tz, parsedate_tz
from tempfile import NamedTemporaryFile

from util import (PY3, URLLIB3_FROM_PIP, LogLevel, fsync, get_supported_encodings, is_dns_working, no_internet, opendir,
                  setup_urllib3_ssl, try_unlink)

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from argparse import Namespace
    from typing import Any, AnyStr, BinaryIO, Callable, Optional, Text

try:
    from urllib.parse import urljoin, urlsplit
except ImportError:
    from urlparse import urljoin, urlsplit  # type: ignore[no-redef]

if URLLIB3_FROM_PIP:
    from pip._vendor.urllib3 import HTTPConnectionPool, HTTPResponse, HTTPSConnectionPool, PoolManager, Timeout
    from pip._vendor.urllib3 import Retry as Retry
    from pip._vendor.urllib3._collections import HTTPHeaderDict
    from pip._vendor.urllib3.exceptions import ConnectTimeoutError, InsecureRequestWarning, MaxRetryError
    from pip._vendor.urllib3.exceptions import HTTPError as HTTPError
    from pip._vendor.urllib3.util import make_headers
else:
    from urllib3 import HTTPConnectionPool, HTTPResponse, HTTPSConnectionPool, PoolManager, Timeout
    from urllib3 import Retry as Retry
    from urllib3._collections import HTTPHeaderDict
    from urllib3.exceptions import ConnectTimeoutError, InsecureRequestWarning, MaxRetryError
    from urllib3.exceptions import HTTPError as HTTPError
    from urllib3.util import make_headers

setup_urllib3_ssl()

# long is int in Python 3
try:
    long  # type: ignore[has-type]
except NameError:
    long = int

HTTP_TIMEOUT = Timeout(90)
# Always retry on 503 or 504, but never on connect, which is handled specially
HTTP_RETRY = Retry(3, connect=False, status_forcelist=frozenset((503, 504)))
HTTP_RETRY.RETRY_AFTER_STATUS_CODES = frozenset((413, 429))
HTTP_CHUNK_SIZE = 1024 * 1024

base_headers = make_headers(keep_alive=True, accept_encoding=list(get_supported_encodings()))


# Document type flags
RETROKF = 0x2             # retrieval was OK


# Error statuses
class UErr(object):
    RETRUNNEEDED = 0
    RETRINCOMPLETE = 1  # Not part of wget
    RETRFINISHED = 2


class HttpStat(object):
    def __init__(self):
        self.current_url = None        # type: Optional[Any] # the most recent redirect, otherwise the initial url
        self.bytes_read = 0            # received length
        self.bytes_written = 0         # written length
        self.contlen = None            # type: Optional[int] # expected length
        self.restval = 0               # the restart value
        self.last_modified = None      # type: Optional[str] # Last-Modified header
        self.remote_time = None        # type: Optional[int] # remote time-stamp
        self.statcode = 0              # status code
        self.dest_dir = None           # type: Optional[int] # handle to the directory containing part_file
        self.part_file = None          # type: Optional[BinaryIO] # handle to local file used for in-progress download
        self.remote_encoding = None    # type: Optional[str] # the encoding of the remote file
        self.enc_is_identity = None    # type: Optional[bool] # whether the remote encoding is identity
        self.decoder = None            # type: Optional[object] # saved decoder from the HTTPResponse
        self._make_part_file = None    # type: Optional[Callable[[], BinaryIO]] # part_file supplier

    def set_part_file_supplier(self, value):
        self._make_part_file = value

    def init_part_file(self):
        if self._make_part_file is not None:
            self.part_file = self._make_part_file()
            self._make_part_file = None


class WGHTTPResponse(HTTPResponse):
    REDIRECT_STATUSES = [300] + HTTPResponse.REDIRECT_STATUSES

    # Make decoder public for saving and restoring the decoder state
    @property
    def decoder(self):
        return self._decoder

    @decoder.setter
    def decoder(self, value):
        self._decoder = value

    def __init__(self, body="", headers=None, status=0, version=0, reason=None, strict=0, preload_content=True,
                 decode_content=True, original_response=None, pool=None, connection=None, msg=None, retries=None,
                 enforce_content_length=False, request_method=None, request_url=None, auto_close=True, **kwargs):
        # Copy original Content-Length for _init_length
        if not isinstance(headers, HTTPHeaderDict):
            headers = HTTPHeaderDict(headers)
        if 'Content-Length' not in headers and 'X-Archive-Orig-Content-Length' in headers:
            headers['Content-Length'] = headers['X-Archive-Orig-Content-Length']

        # Current URL for redirect tracking
        self.current_url = kwargs.pop('current_url')
        assert not kwargs

        self.bytes_to_skip = 0
        self.last_read_length = 0
        super(WGHTTPResponse, self).__init__(
            body, headers, status, version, reason, strict, preload_content, decode_content, original_response, pool,
            connection, msg, retries, enforce_content_length, request_method, request_url, auto_close,
        )

    # Make _init_length publicly usable because its implementation is nice
    def get_content_length(self, meth):
        return self._init_length(meth)

    # Wrap _decode to do some extra processing of the content-encoded entity data.
    def _decode(self, data, decode_content, flush_decoder):
        # Skip any data we don't need
        data_len = len(data)
        if self.bytes_to_skip >= data_len:
            data = b''
            self.bytes_to_skip -= data_len
        elif self.bytes_to_skip > 0:
            data = data[self.bytes_to_skip:]
            self.bytes_to_skip = 0

        self.last_read_length = len(data)  # Count only non-skipped data
        if not data:
            return b''
        return super(WGHTTPResponse, self)._decode(data, decode_content, flush_decoder)


class WGHTTPConnectionPool(HTTPConnectionPool):
    ResponseCls = WGHTTPResponse

    def __init__(self, host, port=None, *args, **kwargs):
        norm_host = normalized_host(self.scheme, host, port)
        cfh_url = kwargs.pop('cfh_url', None)
        if norm_host in unreachable_hosts:
            raise WGUnreachableHostError(None, cfh_url, 'Host {} is ignored.'.format(norm_host))
        super(WGHTTPConnectionPool, self).__init__(host, port, *args, **kwargs)

    def urlopen(self, method, url, *args, **kwargs):
        kwargs['current_url'] = url
        return super(WGHTTPConnectionPool, self).urlopen(method, url, *args, **kwargs)


class WGHTTPSConnectionPool(HTTPSConnectionPool):
    ResponseCls = WGHTTPResponse

    def __init__(self, host, port=None, *args, **kwargs):
        norm_host = normalized_host(self.scheme, host, port)
        cfh_url = kwargs.pop('cfh_url', None)
        if norm_host in unreachable_hosts:
            raise WGUnreachableHostError(None, cfh_url, 'Host {} is ignored.'.format(norm_host))
        super(WGHTTPSConnectionPool, self).__init__(host, port, *args, **kwargs)

    def urlopen(self, method, url, *args, **kwargs):
        kwargs['current_url'] = url
        return super(WGHTTPSConnectionPool, self).urlopen(method, url, *args, **kwargs)


class WGPoolManager(PoolManager):
    def __init__(self, num_pools=10, headers=None, **connection_pool_kw):
        super(WGPoolManager, self).__init__(num_pools, headers, **connection_pool_kw)
        self.cfh_url = None
        self.pool_classes_by_scheme = {'http': WGHTTPConnectionPool, 'https': WGHTTPSConnectionPool}

    def connection_from_url(self, url, pool_kwargs=None):
        try:
            self.cfh_url = url
            return super(WGPoolManager, self).connection_from_url(url, pool_kwargs)
        finally:
            self.cfh_url = None

    def urlopen(self, method, url, redirect=True, **kw):
        try:
            self.cfh_url = url
            return super(WGPoolManager, self).urlopen(method, url, redirect, **kw)
        finally:
            self.cfh_url = None

    def _new_pool(self, scheme, host, port, request_context=None):
        if request_context is None:
            request_context = self.connection_pool_kw.copy()
        request_context['cfh_url'] = self.cfh_url
        return super(WGPoolManager, self)._new_pool(scheme, host, port, request_context)


poolman = WGPoolManager(maxsize=20, timeout=HTTP_TIMEOUT, retries=HTTP_RETRY)


class Logger(object):
    def __init__(self, original_url, log):
        self.original_url = original_url
        self.log_cb = log
        self.prev_log_url = None

    def log(self, level, url, msg):
        qmsg = u''
        if self.prev_log_url is None:
            qmsg += u'[wget] {}URL is {}\n'.format('' if url == self.original_url else 'Original ', self.original_url)
            self.prev_log_url = self.original_url
        if url != self.prev_log_url:
            qmsg += u'[wget] Current redirect URL is {}\n'.format(url)
            self.prev_log_url = url
        qmsg += u'[wget] {}\n'.format(msg)
        self.log_cb(level, qmsg)

    def info(self, url, msg):
        self.log(LogLevel.INFO, url, msg)

    def warn(self, url, msg):
        self.log(LogLevel.WARN, url, msg)

    def error(self, url, msg):
        self.log(LogLevel.ERROR, url, msg)


def gethttp(url, hstat, doctype, logger, retry_counter):
    if hstat.current_url is not None:
        url = hstat.current_url  # The most recent location is cached

    hstat.bytes_read = 0
    hstat.contlen = None
    hstat.remote_time = None

    # Initialize the request
    request_headers = {}
    if hstat.restval:
        request_headers['Range'] = 'bytes={}-'.format(hstat.restval)

    doctype &= ~RETROKF

    resp = urlopen(url, headers=request_headers, preload_content=False, enforce_content_length=False)
    url = hstat.current_url = urljoin(url, resp.current_url)

    try:
        err, doctype = process_response(url, hstat, doctype, logger, retry_counter, resp)
    finally:
        resp.release_conn()

    return err, doctype


def process_response(url, hstat, doctype, logger, retry_counter, resp):
    # RFC 7233 section 4.1 paragraph 6:
    # "A server MUST NOT generate a multipart response to a request for a single range [...]"
    conttype = resp.headers.get('Content-Type')
    if conttype is not None and conttype.lower().split(';', 1)[0].strip() == 'multipart/byteranges':
        raise WGBadResponseError(logger, url, 'Sever sent multipart response, but multiple ranges were not requested')

    contlen = resp.get_content_length('GET')

    crange_header = resp.headers.get('Content-Range')
    crange_parsed = parse_content_range(crange_header)
    if crange_parsed is not None:
        first_bytep, last_bytep, _ = crange_parsed
        contrange = first_bytep
        contlen = last_bytep - first_bytep + 1
    else:
        contrange = 0

    hstat.last_modified = resp.headers.get('Last-Modified')
    if hstat.last_modified is None:
        hstat.last_modified = resp.headers.get('X-Archive-Orig-Last-Modified')

    if hstat.last_modified is None:
        hstat.remote_time = None
    else:
        lmtuple = parsedate_tz(hstat.last_modified)
        hstat.remote_time = None if lmtuple is None else mktime_tz(lmtuple)

    remote_encoding = resp.headers.get('Content-Encoding')

    def norm_enc(enc):
        return None if enc is None else tuple(e.strip() for e in enc.split(','))

    if hstat.restval > 0 and norm_enc(hstat.remote_encoding) != norm_enc(remote_encoding):
        # Retry without restart
        hstat.restval = 0
        retry_counter.increment(hstat, 'Inconsistent Content-Encoding, must start over')
        return UErr.RETRINCOMPLETE, doctype

    hstat.remote_encoding = remote_encoding
    hstat.enc_is_identity = remote_encoding in (None, '') or all(
        enc.strip() == 'identity' for enc in remote_encoding.split(',')
    )

    # In some cases, httplib returns a status of _UNKNOWN
    try:
        hstat.statcode = long(resp.status)
    except ValueError:
        hstat.statcode = 0

    # HTTP 20X
    # HTTP 207 Multi-Status
    if 200 <= hstat.statcode < 300 and hstat.statcode != 207:
        doctype |= RETROKF

    # HTTP 204 No Content
    if hstat.statcode == 204:
        hstat.bytes_read = hstat.restval = 0
        return UErr.RETRFINISHED, doctype

    if not (doctype & RETROKF):
        e = WGWrongCodeError(logger, url, hstat.statcode, resp.reason, resp.headers)
        # Cloudflare-specific errors
        # 521 Web Server Is Down
        # 522 Connection Timed Out
        # 523 Origin Is Unreachable
        # 525 SSL Handshake Failed
        # 526 Invalid SSL Certificate
        if resp.headers.get('Server') == 'cloudflare' and hstat.statcode in (521, 522, 523, 525, 526):
            # Origin is unreachable - condemn it and don't retry
            hostname = normalized_host_from_url(url)
            unreachable_hosts.add(hostname)
            msg = 'Error connecting to origin of host {}. From now on it will be ignored.'.format(hostname)
            raise WGUnreachableHostError(logger, url, msg, e)
        raise e

    shrunk = False
    if hstat.statcode == 416:
        shrunk = True  # HTTP 416 Range Not Satisfiable
    elif hstat.statcode != 200 or contlen == 0:
        pass  # Only verify contlen if 200 OK (NOT 206 Partial Contents) and contlen is nonzero
    elif contlen is not None and contrange == 0 and hstat.restval >= contlen:
        shrunk = True  # Got the whole content but it is known to be shorter than the restart point

    if shrunk:
        # NB: Unlike wget, we will retry because restarts are expected to succeed (we do not support '-c')
        # The remote file has shrunk, retry without restart
        hstat.restval = 0
        retry_counter.increment(hstat, 'Resume with Range failed, must start over')
        return UErr.RETRINCOMPLETE, doctype

    # The Range request was misunderstood. Bail out.
    # Unlike wget, we bail hard with no retry, because this indicates a broken or unreasonable server.
    if contrange not in (0, hstat.restval):
        raise WGRangeError(logger, url, 'Server provided unexpected Content-Range: Requested {}, got {}'
                           .format(hstat.restval, contrange))
    # HTTP 206 Partial Contents
    if hstat.statcode == 206 and hstat.restval > 0 and contrange == 0:
        if crange_header is None:
            crange_status = 'not provided'
        elif crange_parsed is None:
            crange_status = 'invalid'
        else:  # contrange explicitly zero
            crange_status = 'zero'
        raise WGRangeError(logger, url, 'Requested a Range and server sent HTTP 206 Partial Contents, '
                           'but Content-Range is {}!'.format(crange_status))

    hstat.contlen = contlen
    if hstat.contlen is not None:
        hstat.contlen += contrange

    if not (doctype & RETROKF):
        hstat.bytes_read = hstat.restval = 0
        return UErr.RETRFINISHED, doctype

    if hstat.restval > 0 and contrange == 0:
        # If the server ignored our range request, skip the first RESTVAL bytes of the body.
        resp.bytes_to_skip = hstat.restval
    else:
        resp.bytes_to_skip = 0

    hstat.bytes_read = hstat.restval

    assert resp.decoder is None
    if hstat.restval > 0:
        resp.decoder = hstat.decoder  # Resume the previous decoder state -- Content-Encoding is weird

    hstat.init_part_file()  # We're about to write to part_file, make sure it exists

    try:
        for chunk in resp.stream(HTTP_CHUNK_SIZE, decode_content=True):
            hstat.bytes_read += resp.last_read_length
            if not chunk:  # May be possible if not resp.chunked due to implementation of _decode
                continue
            hstat.part_file.write(chunk)
    except MaxRetryError:
        raise
    except (HTTPError, EnvironmentError) as e:
        is_read_error = isinstance(e, HTTPError)
        length_known = hstat.contlen is not None and (is_read_error or hstat.enc_is_identity)
        logger.warn(url, '{} error at byte {}{}'.format(
            'Read' if is_read_error else 'Write',
            hstat.bytes_read if is_read_error else hstat.bytes_written,
            '/{}'.format(hstat.contlen) if length_known else '',
        ))

        if hstat.bytes_read == hstat.restval:
            raise  # No data read
        if not retry_counter.should_retry():
            raise  # This won't be retried

        # Grab the decoder state for next time
        if resp.decoder is not None:
            hstat.decoder = resp.decoder

        # We were able to read at least _some_ body data from the server. Keep trying.
        raise  # Jump to outer except block

    hstat.decoder = None
    return UErr.RETRFINISHED, doctype


def parse_crange_num(hdrc, ci, postchar):
    if not hdrc[ci].isdigit():
        raise ValueError('parse error')
    num = long(0)
    while hdrc[ci].isdigit():
        num = long(10) * num + long(hdrc[ci])
        ci += 1
    if hdrc[ci] != postchar:
        raise ValueError('parse error')
    ci += 1
    return ci, num


def parse_content_range(hdr):
    if hdr is None:
        return None

    # Ancient version of Netscape proxy server don't have the "bytes" specifier
    if hdr.startswith('bytes'):
        hdr = hdr[5:]
        # JavaWebServer/1.1.1 sends "bytes: x-y/z"
        if hdr.startswith(':'):
            hdr = hdr[1:]
        hdr = hdr.lstrip()
        if not hdr:
            return None

    ci = 0
    # Final string is a sentinel, equivalent to a null terminator
    hdrc = tuple(itertools.chain((c for c in hdr), ('',)))

    try:
        ci, first_bytep = parse_crange_num(hdrc, ci, '-')
        ci, last_bytep = parse_crange_num(hdrc, ci, '/')
    except ValueError:
        return None

    if hdrc[ci] == '*':
        entity_length = None
    else:
        num_ = long(0)
        while hdrc[ci].isdigit():
            num_ = long(10) * num_ + long(hdrc[ci])
            ci += 1
        entity_length = num_

    # A byte-content-range-spec whose last-byte-pos value is less than its first-byte-pos value, or whose entity-length
    # value is less than or equal to its last-byte-pos value, is invalid.
    if last_bytep < first_bytep or (entity_length is not None and entity_length <= last_bytep):
        return None

    return first_bytep, last_bytep, entity_length


def touch(fl, mtime, dir_fd=None):
    atime = time.time()
    if PY3 and os.utime in os.supports_dir_fd and dir_fd is not None:
        os.utime(os.path.basename(fl), (atime, mtime), dir_fd=dir_fd)
    else:
        os.utime(fl, (atime, mtime))


class WGError(Exception):
    def __init__(self, logger, url, msg, cause=None):
        super(WGError, self).__init__('Error retrieving resource: {}{}'.format(
            msg, '' if cause is None else '\nCaused by: {}'.format(cause),
        ))
        self.logger = logger
        self.url = url

    def log(self):
        self.logger.warn(self.url, self)


class WGMaxRetryError(WGError):
    pass


class WGUnreachableHostError(WGError):
    pass


class WGBadProtocolError(WGError):
    pass


class WGBadResponseError(WGError):
    pass


class WGWrongCodeError(WGBadResponseError):
    def __init__(self, logger, url, statcode, statmsg, headers):
        msg = 'Unexpected response status: HTTP {} {}{}'.format(
            statcode, statmsg, '' if statcode in (403, 404) else '\nHeaders: {}'.format(headers),
        )
        super(WGWrongCodeError, self).__init__(logger, url, msg)
        self.statcode = statcode
        self.statmsg = statmsg


class WGRangeError(WGBadResponseError):
    pass


unreachable_hosts = set()


class RetryCounter(object):
    TRY_LIMIT = 20
    MAX_RETRY_WAIT = 10

    def __init__(self, logger):
        self.logger = logger
        self.count = 0

    def reset(self):
        self.count = 0

    def should_retry(self):
        return self.TRY_LIMIT is None or self.count < self.TRY_LIMIT

    def increment(self, url, hstat, cause):
        self.count += 1
        status = 'incomplete' if hstat.bytes_read > hstat.restval else 'failed'
        msg = 'because of {} retrieval: {}'.format(status, cause)
        if not self.should_retry():
            self.logger.warn(url, 'Gave up {}'.format(msg))
            raise WGMaxRetryError(self.logger, url, 'Retrieval failed after {} tries.'.format(self.TRY_LIMIT), cause)
        trylim = '' if self.TRY_LIMIT is None else '/{}'.format(self.TRY_LIMIT)
        self.logger.info(url, 'Retrying ({}{}) {}'.format(self.count, trylim, msg))
        time.sleep(min(self.count, self.MAX_RETRY_WAIT))


def normalized_host_from_url(url):
    split = urlsplit(url, 'http')
    hostname = split.hostname
    port = split.port
    if port is None:
        port = 80 if split.scheme == 'http' else 443
    return '{}:{}'.format(hostname, port)


def normalized_host(scheme, host, port):
    if port is None:
        port = 80 if scheme == 'http' else 443
    return '{}:{}'.format(host, port)


def _retrieve_loop(hstat, url, dest_file, post_timestamp, adjust_basename, options, log):
    # type: (HttpStat, AnyStr, AnyStr, Optional[float], Optional[Callable[[AnyStr, BinaryIO], AnyStr]], Namespace, Callable[[Text], None]) -> None  # noqa: E501
    if PY3 and (isinstance(url, bytes) or isinstance(dest_file, bytes)):
        raise ValueError('This function does not support bytes arguments on Python 3')

    logger = Logger(url, log)

    if urlsplit(url).scheme not in ('http', 'https'):
        raise WGBadProtocolError(logger, url, 'Non-HTTP(S) protocols are not implemented.')

    hostname = normalized_host_from_url(url)
    if hostname in unreachable_hosts:
        raise WGUnreachableHostError(logger, url, 'Host {} is ignored.'.format(hostname))

    doctype = 0
    dest_dirname, dest_basename = os.path.split(dest_file)

    if os.name == 'posix':  # Opening directories is a POSIX feature
        hstat.dest_dir = opendir(dest_dirname, os.O_RDONLY)
    hstat.set_part_file_supplier(functools.partial(
        lambda pfx, dir_: NamedTemporaryFile('wb', prefix=pfx, dir=dir_, delete=False),
        '.{}.'.format(dest_basename), dest_dirname,  # type: ignore[str-bytes-safe]
    ))

    # THE loop

    using_internet_archive = False
    ia_fallback_cause = None
    orig_url = url
    orig_doctype = doctype
    retry_counter = RetryCounter(logger)
    while True:
        # Behave as if force_full_retrieve is always enabled
        hstat.restval = hstat.bytes_read

        try:
            err, doctype = gethttp(url, hstat, doctype, logger, retry_counter)
        except MaxRetryError as e:
            raise WGMaxRetryError(logger, url, 'urllib3 reached a retry limit.', e)
        except HTTPError as e:
            if isinstance(e, ConnectTimeoutError):
                # Host is unreachable (incl ETIMEDOUT, EHOSTUNREACH, and EAI_NONAME) - condemn it and don't retry
                hostname = normalized_host_from_url(url)
                unreachable_hosts.add(hostname)
                msg = 'Error connecting to host {}. From now on it will be ignored.'.format(hostname)
                raise WGUnreachableHostError(logger, url, msg, e)

            retry_counter.increment(url, hstat, repr(e))
            continue
        except WGUnreachableHostError as e:
            # Set the logger for unreachable host errors thrown from WGHTTP(S)ConnectionPool
            if e.logger is None:
                e.logger = logger
            raise
        except WGWrongCodeError as e:
            if (options.internet_archive
                and not using_internet_archive
                and hstat.statcode in (403, 404)
                and urlsplit(orig_url).netloc.endswith('.tumblr.com')  # type: ignore[arg-type]
            ):
                using_internet_archive = True
                ia_fallback_cause = (e.args[0], e.statcode, e.statmsg)
                url = 'https://web.archive.org/web/0/{}'.format(orig_url)  # type: ignore[assignment,str-bytes-safe]
                doctype = orig_doctype
                retry_counter.reset()
                continue
            if using_internet_archive and hstat.statcode == 404:
                # Not available at the Internet Archive, report the original error
                assert options.internet_archive
                assert ia_fallback_cause is not None
                msg, statcode, statmsg = ia_fallback_cause
                oe = Exception.__new__(WGWrongCodeError)
                Exception.__init__(oe, msg)
                oe.logger = logger
                oe.url = orig_url
                oe.statcode = statcode
                oe.statmsg = statmsg
                raise oe
            raise
        finally:
            if hstat.current_url is not None:
                url = hstat.current_url

        if err == UErr.RETRINCOMPLETE:
            continue  # Non-fatal error, try again
        if err == UErr.RETRUNNEEDED:
            return
        assert err == UErr.RETRFINISHED

        if hstat.contlen is not None and hstat.bytes_read < hstat.contlen:
            # We lost the connection too soon
            retry_counter.increment(url, hstat, 'Server closed connection before Content-Length was reached.')
            continue

        # We shouldn't have read more than Content-Length bytes
        assert hstat.contlen in (None, hstat.bytes_read)

        if using_internet_archive:
            assert options.internet_archive
            assert ia_fallback_cause is not None
            _, statcode, statmsg = ia_fallback_cause
            logger.info(orig_url, 'Downloaded from Internet Archive due to HTTP Error {} {}'.format(statcode, statmsg))

        # Normal return path - we wrote a local file
        assert hstat.part_file is not None
        pfname = hstat.part_file.name

        # NamedTemporaryFile is created 0600, set mode to the usual 0644
        if os.name == 'posix':
            os.fchmod(hstat.part_file.fileno(), 0o644)
        else:
            os.chmod(hstat.part_file.name, 0o644)

        if options.use_server_timestamps and hstat.remote_time is None:
            logger.warn(url, 'Warning: Last-Modified header is {}'
                       .format('missing' if hstat.last_modified is None
                               else 'invalid: {}'.format(hstat.last_modified)))

        # Flush the userspace buffer so mtime isn't updated
        hstat.part_file.flush()

        # Set the timestamp on the local file
        if (options.use_server_timestamps
            and (hstat.remote_time is not None or post_timestamp is not None)
            and hstat.contlen in (None, hstat.bytes_read)
        ):
            if hstat.remote_time is None:
                tstamp = post_timestamp
            elif post_timestamp is None:
                tstamp = hstat.remote_time
            else:
                tstamp = min(hstat.remote_time, post_timestamp)
            touch(pfname, tstamp, dir_fd=hstat.dest_dir)

        # Adjust the new name
        if adjust_basename is None:
            new_dest_basename = dest_basename
        else:
            # Give adjust_basename a read-only file handle
            pf = io.open(hstat.part_file.fileno(), 'rb', closefd=False)
            new_dest_basename = adjust_basename(dest_basename, pf)  # type: ignore[arg-type]

        # Sync the inode
        fsync(hstat.part_file)
        try:
            hstat.part_file.close()
        finally:
            hstat.part_file = None

        # Move to final destination
        new_dest = os.path.join(dest_dirname, new_dest_basename)
        if not PY3:
            if os.name == 'nt':
                try_unlink(new_dest)  # Avoid potential FileExistsError
            os.rename(pfname, new_dest)
        elif os.rename not in os.supports_dir_fd:
            os.replace(pfname, new_dest)
        else:
            os.replace(os.path.basename(pfname), new_dest_basename,
                       src_dir_fd=hstat.dest_dir, dst_dir_fd=hstat.dest_dir)

        return


def setup_wget(ssl_verify, user_agent):
    if not ssl_verify:
        # Hide the InsecureRequestWarning from urllib3
        warnings.filterwarnings('ignore', category=InsecureRequestWarning)
    poolman.connection_pool_kw['cert_reqs'] = 'CERT_REQUIRED' if ssl_verify else 'CERT_NONE'
    if user_agent is not None:
        base_headers['User-Agent'] = user_agent


# This is a simple urllib3-based urlopen function.
def urlopen(url, method='GET', headers=None, **kwargs):
    req_headers = base_headers.copy()
    if headers is not None:
        req_headers.update(headers)

    while True:
        try:
            return poolman.request(method, url, headers=req_headers, **kwargs)
        except HTTPError:
            if is_dns_working(timeout=5):
                raise
            # Having no internet is a temporary system error
            no_internet.signal()


# This functor is the primary API of this module.
class WgetRetrieveWrapper(object):
    def __init__(self, options, log):
        self.options = options
        self.log = log

    def __call__(self, url, file, post_timestamp=None, adjust_basename=None):
        hstat = HttpStat()
        try:
            _retrieve_loop(hstat, url, file, post_timestamp, adjust_basename, self.options, self.log)
        finally:
            if hstat.dest_dir is not None:
                os.close(hstat.dest_dir)
                hstat.dest_dir = None
            # part_file may still be around if we didn't move it
            if hstat.part_file is not None:
                self._close_part(hstat)

        return hstat

    @staticmethod
    def _close_part(hstat):
        try:
            hstat.part_file.close()
            try_unlink(hstat.part_file.name)
        finally:
            hstat.part_file = None
