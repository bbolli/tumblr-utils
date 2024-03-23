from __future__ import annotations

import re
import sys
import time
import traceback
import warnings
from datetime import datetime
from multiprocessing.queues import SimpleQueue
from typing import cast
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag
from requests.exceptions import RequestException
from urllib3 import Retry, Timeout
from urllib3.exceptions import HTTPError, InsecureRequestWarning

from .util import ConnectionFile, LogLevel, is_dns_working, make_requests_session, setup_urllib3_ssl, to_bytes

setup_urllib3_ssl()

EXIT_SUCCESS = 0
EXIT_SAFE_MODE = 2
EXIT_NO_INTERNET = 3

HTTP_TIMEOUT = Timeout(90)
# Always retry on 503 or 504, but never on connect or 429, the latter handled specially
HTTP_RETRY = Retry(3, connect=False, status_forcelist=frozenset((503, 504)))
HTTP_RETRY.RETRY_AFTER_STATUS_CODES = frozenset((413,))  # type: ignore[misc]

# Globals
post_url = None
ident = None
msg_queue: SimpleQueue[tuple[int, str]] | None = None


def log(level, url, msg):
    assert msg_queue is not None
    url_msg = ", URL '{}'".format(url) if url != post_url else ''
    # see https://github.com/google/pytype/issues/1344#issuecomment-1553500779
    msg_queue.put(  # pytype: disable=attribute-error
        (level, '[Note Scraper] Post {}{}: {}\n'.format(ident, url_msg, msg)),
    )


class WebCrawler:
    # Python 2.x urllib.always_safe is private in Python 3.x; its content is copied here
    _ALWAYS_SAFE_BYTES = (b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                          b'abcdefghijklmnopqrstuvwxyz'
                          b'0123456789' b'_.-')

    _reserved = b';/?:@&=+$|,#'  # RFC 3986 (Generic Syntax)
    _unreserved_marks = b"-_.!~*'()"  # RFC 3986 sec 2.3
    _safe_chars = _ALWAYS_SAFE_BYTES + b'%' + _reserved + _unreserved_marks

    TRY_LIMIT = 10  # max attempts when ratelimited

    def __init__(self, noverify, user_agent, cookiefile, notes_limit):
        self.notes_limit = notes_limit
        self.lasturl = None
        self.session = make_requests_session(
            requests.Session, HTTP_RETRY, HTTP_TIMEOUT, not noverify, user_agent, cookiefile,
        )
        self.original_post_seen = False

    @classmethod
    def quote_unsafe(cls, string):
        return quote(to_bytes(string), cls._safe_chars)

    # Based on w3lib.safe_url_string
    @classmethod
    def iri_to_uri(cls, iri):
        parts = urlsplit(iri)

        # IDNA encoding can fail for too long labels (>63 characters) or missing labels (e.g. http://.example.com)
        try:
            netloc = parts.netloc.encode('idna').decode('ascii')
        except UnicodeError:
            netloc = parts.netloc

        return urlunsplit((
            parts.scheme,
            netloc.rstrip(':'),
            *(cls.quote_unsafe(getattr(parts, p)) for p in ('path', 'query', 'fragment')),
        ))

    def ratelimit_sleep(self, status_code, headers):
        if status_code == 420:  # 'Enhance Your Calm' has no suggested delay
            log(LogLevel.WARN, self.lasturl, 'Rate limited, sleeping for one minute')
            time.sleep(60)
            return True

        reset = headers.get('X-Rate-Limit-Reset')
        if reset is None:
            return False

        # there's a comma, but both numbers seem to be the same for now
        if ',' in reset:
            reset, tmp = reset.split(',', 1)
            assert reset == tmp

        try:
            reset_time = int(reset)
        except ValueError:
            log(LogLevel.ERROR, self.lasturl, "Expected integer X-Rate-Limit-Reset, got '{}'".format(reset))
            return False

        # This header is apparently a unix timestamp
        sleep_dur = (datetime.fromtimestamp(reset_time) - datetime.now()).total_seconds()

        if sleep_dur < 0:
            log(LogLevel.WARN, self.lasturl, 'Warning: X-Rate-Limit-Reset is {} seconds in the past'.format(-sleep_dur))
            return True
        if sleep_dur > 3600:
            log(LogLevel.ERROR, self.lasturl,
                'Refusing to sleep for {} minutes, giving up'.format(round(sleep_dur / 60)))
            return False

        log(LogLevel.WARN, self.lasturl, 'Rate limited, sleeping for {:.2f} seconds as requested'.format(sleep_dur))
        time.sleep(sleep_dur)
        return True

    def urlopen(self, iri):
        self.lasturl = iri
        uri = self.iri_to_uri(iri)

        try_count = 0
        while True:
            with self.session.get(uri) as resp:
                try_count += 1
                parsed_uri = urlparse(resp.url)
                if (
                    re.match(r'(www\.)?tumblr\.com', parsed_uri.netloc)
                    and re.match(r'/safe-mode$|/[a-z0-9-]+/[0-9]+(/|$)', parsed_uri.path)
                ):
                    sys.exit(EXIT_SAFE_MODE)
                if (
                    resp.status_code in (420, 429) and try_count < self.TRY_LIMIT
                    and self.ratelimit_sleep(resp.status_code, resp.headers)
                ):
                    continue
                if 200 <= resp.status_code < 300:
                    return resp.content.decode('utf-8', errors='ignore')
                log(LogLevel.WARN, iri, 'Unexpected response status: HTTP {} {}{}'.format(
                    resp.status_code, resp.reason,
                    '' if resp.status_code == 404 else '\nHeaders: {}'.format(resp.headers),
                ))
                return None

    @staticmethod
    def get_more_link(soup, base, notes_url):
        global ident
        element = cast(Tag, soup.find('a', class_='more_notes_link'))
        if not element:
            return None
        onclick = element.get_attribute_list('onclick')[0]
        if not onclick:
            log(LogLevel.WARN, notes_url, 'No onclick attribute, probably a dashboard-only blog')
            return None
        match_ = re.search(r";tumblrReq\.open\('GET','([^']+)'", onclick)
        if not match_:
            log(LogLevel.ERROR, notes_url, 'tumblrReq regex failed, did Tumblr update?')
            return None
        url = urljoin(base, match_.group(1))
        spl = urlsplit(url)
        query = parse_qs(spl.query)
        try:
            del query['large']
        except KeyError:
            pass
        return urlunsplit(spl._replace(query=urlencode(query, doseq=True)))

    def append_notes(self, soup, notes_list, notes_url):
        notes_ol = cast(Tag, soup.find('ol', class_='notes'))
        if notes_ol is None:
            log(LogLevel.WARN, notes_url, 'Response HTML does not have a notes list')
            return False
        notes = notes_ol.find_all('li')
        for note in reversed(notes):
            classes = note.get('class', [])
            if 'more_notes_link_container' in classes:
                continue  # skip more notes link
            if 'original_post' in classes:
                if self.original_post_seen:
                    continue  # only show original post once
                self.original_post_seen = True
            notes_list.append(note.prettify())
        return True

    def get_notes(self, post_url):
        parsed_uri = urlparse(post_url)
        base = '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)

        notes_10k = 0
        notes_list: list[str] = []

        notes_url = post_url
        while True:
            resp_str = self.urlopen(notes_url)
            if resp_str is None:
                break

            soup = BeautifulSoup(resp_str, 'lxml')
            if not self.append_notes(soup, notes_list, notes_url):
                break

            old_notes_url, notes_url = notes_url, self.get_more_link(soup, base, notes_url)
            if (not notes_url) or notes_url == old_notes_url:
                break

            if len(notes_list) > (notes_10k + 1) * 10000:
                notes_10k += 1
                log(LogLevel.INFO, notes_url, 'Note: {} notes retrieved so far'.format(notes_10k * 10000))
            if self.notes_limit is not None and len(notes_list) > self.notes_limit:
                log(LogLevel.WARN, notes_url, 'Warning: Reached notes limit, stopping early.')
                break

        return ''.join(notes_list)


def main(stdout_conn, msg_queue_, post_url_, ident_, noverify, user_agent, cookiefile, notes_limit, use_dns_check):
    global post_url, ident, msg_queue
    msg_queue, post_url, ident = msg_queue_, post_url_, ident_

    assert msg_queue is not None
    msg_queue._reader.close()  # type: ignore[attr-defined]

    if noverify:
        # Hide the InsecureRequestWarning from urllib3
        warnings.filterwarnings('ignore', category=InsecureRequestWarning)

    try:
        crawler = WebCrawler(noverify, user_agent, cookiefile, notes_limit)

        try:
            notes = crawler.get_notes(post_url)
        except KeyboardInterrupt:
            sys.exit()  # Ignore these so they don't propogate into the parent
        except (HTTPError, RequestException) as e:
            if not is_dns_working(timeout=5, check=use_dns_check):
                sys.exit(EXIT_NO_INTERNET)
            log(LogLevel.ERROR, crawler.lasturl, e)
            sys.exit()
        except Exception:
            log(LogLevel.ERROR, crawler.lasturl, 'Caught an exception\n{}'.format(traceback.format_exc()))
            sys.exit()
    finally:
        msg_queue._writer.close()  # type: ignore[attr-defined]

    with ConnectionFile(stdout_conn, 'w') as stdout:
        print(notes, end='', file=stdout)
