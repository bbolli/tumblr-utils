from __future__ import annotations

import errno
import functools
import itertools
import os
import time
import traceback
import warnings
from argparse import Namespace
from collections import OrderedDict
from email.utils import mktime_tz, parsedate_tz
from enum import Enum
from http.client import (HTTPConnection as _HTTPConnection, HTTPMessage as _HttplibHTTPMessage,
                         HTTPResponse as _HttplibHTTPResponse, ResponseNotReady)
from tempfile import NamedTemporaryFile
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Callable, Dict, Iterable, Mapping, Optional, Set
from urllib.parse import urljoin, urlsplit

from urllib3 import (BaseHTTPResponse, HTTPConnectionPool, HTTPHeaderDict, HTTPResponse, HTTPSConnectionPool,
                     PoolManager, Retry as Retry, Timeout, make_headers)
from urllib3.connection import HTTPConnection, HTTPSConnection, _url_from_connection  # noqa: WPS450
from urllib3.exceptions import (ConnectTimeoutError, HeaderParsingError, HTTPError as HTTPError, InsecureRequestWarning,
                                MaxRetryError)
from urllib3.util.response import assert_header_parsing

from .util import LogLevel, enospc, fsync, is_dns_working, no_internet, opendir, setup_urllib3_ssl, try_unlink

if TYPE_CHECKING:
    from typing_extensions import TypeAlias

TYPE_BODY: TypeAlias = 'bytes | IO[Any] | Iterable[bytes] | str'

setup_urllib3_ssl()

HTTP_TIMEOUT = Timeout(90)
# Always retry on 503 or 504, but never on connect, which is handled specially
# Also retry on 500 and 502 since Tumblr servers have temporary failures
HTTP_RETRY = Retry(3, connect=False, status_forcelist=frozenset((500, 502, 503, 504)))
HTTP_RETRY.RETRY_AFTER_STATUS_CODES = frozenset((413, 429))  # type: ignore[misc]
HTTP_CHUNK_SIZE = 1024 * 1024

base_headers = make_headers(keep_alive=True, accept_encoding=True)


# Document type flags
RETROKF = 0x2             # retrieval was OK


# Error statuses
class UErr(Enum):
    RETRUNNEEDED = 0
    RETRINCOMPLETE = 1
    RETRFINISHED = 2


class HttpStat:
    current_url: Optional[Any]
    contlen: Optional[int]
    last_modified: Optional[str]
    remote_time: Optional[int]
    dest_dir: Optional[int]
    part_file: Optional[BinaryIO]
    remote_encoding: Optional[str]
    enc_is_identity: Optional[bool]
    decoder: Optional[object]
    _make_part_file: Optional[Callable[[], BinaryIO]]

    def __init__(self):
        self.current_url = None      # the most recent redirect, otherwise the initial url
        self.bytes_read = 0          # received length
        self.bytes_written = 0       # written length
        self.contlen = None          # expected length
        self.restval = 0             # the restart value
        self.last_modified = None    # Last-Modified header
        self.remote_time = None      # remote time-stamp
        self.statcode = 0            # status code
        self.dest_dir = None         # handle to the directory containing part_file
        self.part_file = None        # handle to local file used for in-progress download
        self.remote_encoding = None  # the encoding of the remote file
        self.enc_is_identity = None  # whether the remote encoding is identity
        self.decoder = None          # saved decoder from the HTTPResponse
        self._make_part_file = None  # part_file supplier

    def set_part_file_supplier(self, value):
        self._make_part_file = value

    def init_part_file(self):
        if self._make_part_file is not None:
            self.part_file = self._make_part_file()
            self._make_part_file = None


class WGHTTPResponse(HTTPResponse):
    REDIRECT_STATUSES = [300] + HTTPResponse.REDIRECT_STATUSES

    def __init__(
        self,
        body                   : TYPE_BODY                   = '',
        headers                : Mapping[str, str] | None    = None,  # NB: cannot be bytes!
        status                 : int                         = 0,
        version                : int                         = 0,
        version_string         : str                         = 'HTTP/?',
        reason                 : str | None                  = None,
        preload_content        : bool                        = True,
        decode_content         : bool                        = True,
        original_response      : _HttplibHTTPResponse | None = None,
        pool                   : WGHTTPConnectionPool | None = None,
        connection             : WGHTTPConnection     | None = None,
        msg                    : _HttplibHTTPMessage  | None = None,
        retries                : Retry | None                = None,
        enforce_content_length : bool                        = False,  # NB: different default!
        request_method         : str | None                  = None,
        request_url            : str | None                  = None,
        auto_close             : bool                        = True,
    ):
        # Copy original Content-Length for _init_length
        if not isinstance(headers, HTTPHeaderDict):
            headers = HTTPHeaderDict(headers)  # type: ignore[arg-type]
        if 'Content-Length' not in headers and 'X-Archive-Orig-Content-Length' in headers:
            header_dict = dict(headers)
            header_dict['Content-Length'] = header_dict['X-Archive-Orig-Content-Length']
            headers = header_dict

        self.bytes_to_skip = 0
        self.last_read_length = 0
        super().__init__(
            body=body, headers=headers, status=status, version=version, version_string=version_string, reason=reason,
            preload_content=preload_content, decode_content=decode_content, original_response=original_response,
            pool=pool, connection=connection, msg=msg, retries=retries, enforce_content_length=enforce_content_length,
            request_method=request_method, request_url=request_url, auto_close=auto_close,
        )

    # Make decoder public for saving and restoring the decoder state
    @property
    def decoder(self):
        return self._decoder  # pytype: disable=attribute-error

    @decoder.setter
    def decoder(self, value):
        self._decoder = value

    # Make _init_length publicly usable because its implementation is nice
    def get_content_length(self, meth):
        return self._init_length(meth)  # type: ignore[attr-defined]

    def _init_decoder(self) -> None:
        self.last_read_length = 0
        super()._init_decoder()

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

        self.last_read_length += len(data)  # Count only non-skipped data
        if not data:
            data = b''
            if flush_decoder:
                data += self._flush_decoder()
            return data
        return super()._decode(data, decode_content, flush_decoder)  # type: ignore[misc]


class WGHTTPConnection(HTTPConnection):
    def getresponse(self) -> WGHTTPResponse:  # type: ignore[override]
        # Raise the same error as http.client.HTTPConnection
        if self._response_options is None:
            raise ResponseNotReady()

        # Reset this attribute for being used again.
        resp_options = self._response_options
        self._response_options = None

        # Since the connection's timeout value may have been updated
        # we need to set the timeout on the socket.
        self.sock.settimeout(self.timeout)

        # Get the response from http.client.HTTPConnection
        httplib_response = _HTTPConnection.getresponse(self)

        try:
            assert_header_parsing(httplib_response.msg)
        except (HeaderParsingError, TypeError) as hpe:
            print('Failed to parse headers (url={}): {}'.format(
                _url_from_connection(self, resp_options.request_url), hpe,
            ))
            traceback.print_exc()

        headers = HTTPHeaderDict(httplib_response.msg.items())

        return WGHTTPResponse(
            body=httplib_response,
            headers=headers,
            status=httplib_response.status,
            version=httplib_response.version,
            reason=httplib_response.reason,
            preload_content=resp_options.preload_content,
            decode_content=resp_options.decode_content,
            original_response=httplib_response,
            enforce_content_length=resp_options.enforce_content_length,
            request_method=resp_options.request_method,
            request_url=resp_options.request_url,
        )


class WGHTTPSConnection(WGHTTPConnection, HTTPSConnection):
    pass


class WGHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = WGHTTPConnection

    def __init__(self, host, port=None, *args, **kwargs):
        norm_host = normalized_host(self.scheme, host, port)
        cfh_url = kwargs.pop('cfh_url', None)
        if norm_host in unreachable_hosts:
            raise WGUnreachableHostError(None, cfh_url, 'Host {} is ignored.'.format(norm_host))
        super().__init__(host, port, *args, **kwargs)


class WGHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = WGHTTPSConnection

    def __init__(self, host, port=None, *args, **kwargs):
        norm_host = normalized_host(self.scheme, host, port)
        cfh_url = kwargs.pop('cfh_url', None)
        if norm_host in unreachable_hosts:
            raise WGUnreachableHostError(None, cfh_url, 'Host {} is ignored.'.format(norm_host))
        super().__init__(host, port, *args, **kwargs)


class WGPoolManager(PoolManager):
    def __init__(self, num_pools=10, headers=None, **connection_pool_kw):
        super().__init__(num_pools, headers, **connection_pool_kw)
        self.cfh_url = None
        self.pool_classes_by_scheme = {'http': WGHTTPConnectionPool, 'https': WGHTTPSConnectionPool}

    def connection_from_url(self, url, pool_kwargs=None):
        try:
            self.cfh_url = url
            return super().connection_from_url(url, pool_kwargs)  # type: ignore[call-arg]
        finally:
            self.cfh_url = None

    # the urllib3 stubs lie about this method's signature
    def urlopen(self, method, url, redirect=True, **kw):  # type: ignore[override] # pytype: disable=signature-mismatch
        try:
            self.cfh_url = url
            return super().urlopen(method, url, redirect, **kw)
        finally:
            self.cfh_url = None

    def _new_pool(self, scheme, host, port, request_context=None):
        if request_context is None:
            request_context = self.connection_pool_kw.copy()
        request_context['cfh_url'] = self.cfh_url
        return super()._new_pool(scheme, host, port, request_context)  # type: ignore[misc]


poolman = WGPoolManager(maxsize=20, timeout=HTTP_TIMEOUT)


class Logger:
    def __init__(self, original_url, post_id, log):
        self.original_url = original_url
        self.post_id = post_id
        self.log_cb = log

    def info(self, url, msg):
        self._log_info(LogLevel.INFO, url, msg)

    def warn(self, url, msg):
        self._log_info(LogLevel.WARN, url, msg)

    def error(self, url, msg, info):
        qmsg = '[wget] Error retrieving media\n'
        qmsg += '  {}\n'.format(msg)
        if self.post_id is not None:
            info['Post'] = self.post_id

        url_key = 'URL' if url == self.original_url else 'Original URL'
        info[url_key] = self.original_url
        if url != self.original_url:
            info['Redirect URL'] = url

        for k, v in info.items():
            qmsg += '  {}: {}\n'.format(k, v)

        self.log_cb(LogLevel.WARN, qmsg)  # wget errors can still be silenced

    def _log_info(self, level, url, msg):
        qmsg = '[wget] {}\n'.format(msg)
        qmsg += '  URL{}: {}\n'.format(
            '' if url == self.original_url else ' (redirect)',
            url,
        )
        self.log_cb(level, qmsg)


def gethttp(url, hstat, doctype, logger, retry_counter, use_dns_check):
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

    resp = urlopen(url, use_dns_check, request_headers, preload_content=False, enforce_content_length=False)
    url = hstat.current_url = urljoin(url, resp.geturl())

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
        retry_counter.increment(url, hstat, 'Inconsistent Content-Encoding, must start over')
        return UErr.RETRINCOMPLETE, doctype

    hstat.remote_encoding = remote_encoding
    hstat.enc_is_identity = remote_encoding in (None, '') or all(
        enc.strip() == 'identity' for enc in remote_encoding.split(',')
    )

    # In some cases, httplib returns a status of _UNKNOWN
    try:
        hstat.statcode = int(resp.status)
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

    # HTTP 420 Enhance Your Calm
    if hstat.statcode == 420:
        retry_counter.increment(url, hstat, 'Rate limited (HTTP 420 Enhance Your Calm)', 60)
        logger.info(url, 'Rate limited, sleeping for one minute')
        return UErr.RETRINCOMPLETE, doctype

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
        retry_counter.increment(url, hstat, 'Resume with Range failed, must start over')
        return UErr.RETRINCOMPLETE, doctype

    # The Range request was misunderstood. Bail out.
    # Unlike wget, we bail hard with no retry, because this indicates a broken or unreasonable server.
    if contrange not in (0, hstat.restval):
        raise WGRangeError(
            logger, url,
            f'Server provided unexpected Content-Range: Requested {hstat.restval}, got {contrange}',
        )
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
    assert hstat.part_file is not None

    try:
        for chunk in resp.stream(HTTP_CHUNK_SIZE, decode_content=True):
            hstat.bytes_read += resp.last_read_length
            if not chunk:  # May be possible if not resp.chunked due to implementation of _decode
                continue
            hstat.part_file.write(chunk)
    except MaxRetryError:
        raise
    except (HTTPError, OSError) as e:
        is_read_error = isinstance(e, HTTPError)
        length_known = hstat.contlen is not None and (is_read_error or hstat.enc_is_identity)
        logger.warn(url, '{} error at byte {}{}'.format(
            'Read' if is_read_error else 'Write',
            hstat.bytes_read if is_read_error else hstat.bytes_written,
            '/{}'.format(hstat.contlen) if length_known else '',
        ))

        if hstat.bytes_read == hstat.restval:
            raise  # No data read
        if isinstance(e, OSError) and e.errno == errno.ENOSPC:
            raise  # Handled specialy in outer except block
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
    num = 0
    while hdrc[ci].isdigit():
        num = 10 * num + int(hdrc[ci])
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
        num_ = int(0)
        while hdrc[ci].isdigit():
            num_ = int(10) * num_ + int(hdrc[ci])
            ci += 1
        entity_length = num_

    # A byte-content-range-spec whose last-byte-pos value is less than its first-byte-pos value, or whose entity-length
    # value is less than or equal to its last-byte-pos value, is invalid.
    if last_bytep < first_bytep or (entity_length is not None and entity_length <= last_bytep):
        return None

    return first_bytep, last_bytep, entity_length


def touch(fl, mtime, dir_fd=None):
    atime = time.time()
    if os.utime in os.supports_dir_fd and dir_fd is not None:
        os.utime(os.path.basename(fl), (atime, mtime), dir_fd=dir_fd)
    else:
        os.utime(fl, (atime, mtime))


class WGError(Exception):
    def __init__(self, logger, url, msg, cause=None, info=None):
        self.logger = logger
        self.url = url
        self.msg = msg
        self.cause = cause
        self.info = info

    def log(self):
        info = OrderedDict()
        if self.cause is not None:
            info['Caused by'] = repr(self.cause)
        if self.info is not None:
            info.update(self.info)
        self.logger.error(self.url, self.msg, info)

    def __str__(self):
        return repr(self)


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
        msg = 'Unexpected response status: HTTP {} {}'.format(statcode, statmsg)
        info = OrderedDict()
        if statcode not in (403, 404):
            info['Headers'] = headers
        super().__init__(logger, url, msg, info=info)
        self.statcode = statcode
        self.statmsg = statmsg


class WGRangeError(WGBadResponseError):
    pass


unreachable_hosts: Set[str] = set()


class RetryCounter:
    TRY_LIMIT = 20
    MAX_RETRY_WAIT = 10

    def __init__(self, logger):
        self.logger = logger
        self.count = 0

    def reset(self):
        self.count = 0

    def should_retry(self):
        return self.TRY_LIMIT is None or self.count < self.TRY_LIMIT

    def increment(self, url, hstat, cause, sleep_dur=None):
        self.count += 1
        status = 'incomplete' if hstat.bytes_read > hstat.restval else 'failed'
        msg = 'because of {} retrieval: {}'.format(status, cause)
        if not self.should_retry():
            raise WGMaxRetryError(
                self.logger, url,
                'Retrieval {} after {} tries.'.format(status, self.TRY_LIMIT),
                cause,
            )
        trylim = '' if self.TRY_LIMIT is None else '/{}'.format(self.TRY_LIMIT)
        self.logger.info(url, 'Retrying ({}{}) {}'.format(self.count, trylim, msg))

        if sleep_dur is None:
            sleep_dur = min(self.count, self.MAX_RETRY_WAIT)
        time.sleep(sleep_dur)


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


def _retrieve_loop(
    hstat: HttpStat,
    url: str,
    dest_file: str,
    post_id: Optional[str],
    post_timestamp: Optional[float],
    adjust_basename: Optional[Callable[[str, BinaryIO], str]],
    log: Callable[[LogLevel, str], None],
    use_dns_check: bool,
    use_internet_archive: bool,
    use_server_timestamps: bool,
) -> None:
    logger = Logger(url, post_id, log)

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
        '.{}.'.format(dest_basename), dest_dirname,
    ))

    # THE loop

    using_internet_archive = False
    ia_fallback_cause: Optional[WGWrongCodeError] = None
    orig_url = url
    orig_doctype = doctype
    retry_counter = RetryCounter(logger)
    while True:
        # Behave as if force_full_retrieve is always enabled
        hstat.restval = hstat.bytes_read

        try:
            err, doctype = gethttp(url, hstat, doctype, logger, retry_counter, use_dns_check)
        except MaxRetryError as e:
            raise WGMaxRetryError(logger, url, 'urllib3 reached a retry limit.', e)
        except HTTPError as e:
            if isinstance(e, ConnectTimeoutError):
                # Host is unreachable (incl ETIMEDOUT, EHOSTUNREACH, and EAI_NONAME) - condemn it and don't retry
                conn: HTTPConnection | None = None
                if hasattr(e, 'conn') and isinstance(e.conn, HTTPConnection):
                    conn = e.conn
                elif e.args and isinstance(e.args[0], HTTPConnection):
                    conn = e.args[0]
                if conn is not None:
                    hostname = normalized_host(None, conn.host, conn.port)
                    unreachable_hosts.add(hostname)
                    msg = 'Error connecting to host {}. From now on it will be ignored.'.format(hostname)
                    raise WGUnreachableHostError(logger, url, msg, e)

            retry_counter.increment(url, hstat, repr(e))
            continue
        except OSError as e:
            if e.errno != errno.ENOSPC:
                raise

            # Being low on disk space is a temporary system error, don't count against the server
            enospc.signal()
            continue
        except WGUnreachableHostError as e:
            # Set the logger for unreachable host errors thrown from WGHTTP(S)ConnectionPool
            if e.logger is None:
                e.logger = logger
            raise
        except WGWrongCodeError as e:
            if (
                use_internet_archive
                and not using_internet_archive
                and hstat.statcode in (403, 404)
                and urlsplit(orig_url).netloc.endswith('.tumblr.com')  # type: ignore[arg-type]
            ):
                using_internet_archive = True
                traceback.clear_frames(e.__traceback__)  # prevent reference cycle
                ia_fallback_cause = e
                url = 'https://web.archive.org/web/0/{}'.format(orig_url)  # type: ignore[assignment,str-bytes-safe]
                doctype = orig_doctype
                retry_counter.reset()
                continue
            if using_internet_archive and hstat.statcode == 404:
                # Not available at the Internet Archive, report the original error
                assert ia_fallback_cause is not None
                raise ia_fallback_cause from None
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
            assert ia_fallback_cause is not None
            c = ia_fallback_cause
            logger.info(
                orig_url, 'Downloaded from Internet Archive due to HTTP Error {} {}'.format(c.statcode, c.statmsg),
            )

        # Normal return path - we wrote a local file
        assert hstat.part_file is not None
        pfname = hstat.part_file.name

        # NamedTemporaryFile is created 0600, set mode to the usual 0644
        if os.name == 'posix':
            os.fchmod(hstat.part_file.fileno(), 0o644)
        else:
            os.chmod(hstat.part_file.name, 0o644)

        if use_server_timestamps and hstat.remote_time is None:
            status = 'missing' if hstat.last_modified is None else f'invalid: {hstat.last_modified}'
            logger.warn(url, f'Warning: Last-Modified header is {status}')

        # Flush the userspace buffer so mtime isn't updated
        hstat.part_file.flush()

        # Set the timestamp on the local file
        if (
            use_server_timestamps
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
            pf = open(hstat.part_file.fileno(), 'rb', closefd=False)
            new_dest_basename = adjust_basename(dest_basename, pf)

        # Sync the inode
        fsync(hstat.part_file)
        try:
            hstat.part_file.close()
        finally:
            hstat.part_file = None

        # Move to final destination
        new_dest = os.path.join(dest_dirname, new_dest_basename)
        if os.rename not in os.supports_dir_fd:
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
def urlopen(url, use_dns_check: bool, headers: Optional[Dict[str, str]] = None, **kwargs) -> BaseHTTPResponse:
    req_headers = base_headers.copy()
    if headers is not None:
        req_headers.update(headers)

    while True:
        try:
            return poolman.request('GET', url, headers=req_headers, retries=HTTP_RETRY, **kwargs)
        except HTTPError:
            if is_dns_working(timeout=5, check=use_dns_check):
                raise
            # Having no internet is a temporary system error
            no_internet.signal()


# This functor is the primary API of this module.
class WgetRetrieveWrapper:
    def __init__(self, log: Callable[[LogLevel, str], None], options: Namespace):
        self.log = log
        self.options = options

    def __call__(self, url, file, post_id=None, post_timestamp=None, adjust_basename=None):
        hstat = HttpStat()
        try:
            _retrieve_loop(
                hstat, url, file, post_id, post_timestamp, adjust_basename, self.log,
                use_dns_check=self.options.use_dns_check, use_internet_archive=self.options.internet_archive,
                use_server_timestamps=self.options.use_server_timestamps,
            )
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
