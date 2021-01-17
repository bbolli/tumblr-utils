# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

import contextlib
import io
import itertools
import re
import ssl
import sys
import time
import traceback

from bs4 import BeautifulSoup

from util import HAVE_SSL_CTX, HTTP_TIMEOUT, to_bytes, to_native_str

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import List, Text

try:
    from http.client import HTTPException
except ImportError:
    from httplib import HTTPException  # type: ignore[no-redef]

try:
    from http.cookiejar import MozillaCookieJar
except ImportError:
    from cookielib import MozillaCookieJar  # type: ignore[no-redef]

try:
    from urllib.error import HTTPError, URLError
    from urllib.parse import quote, urlparse, urlsplit, urlunsplit
    from urllib.request import BaseHandler, HTTPCookieProcessor, HTTPSHandler, build_opener
except ImportError:
    from urllib import quote  # type: ignore[attr-defined,no-redef]
    from urllib2 import (BaseHandler, HTTPCookieProcessor, HTTPSHandler, HTTPError, URLError,  # type: ignore[no-redef]
                         build_opener)
    from urlparse import urlparse, urlsplit, urlunsplit  # type: ignore[no-redef]

EXIT_SUCCESS = 0
EXIT_SAFE_MODE = 2


def log(url, msg):
    global msg_pipe
    url_msg = ", URL '{}'".format(url) if url != post_url else ''
    print('[Note Scraper] Post {}{}: {}'.format(ident, url_msg, msg), file=msg_pipe)


class WebCrawler(object):

    # Python 2.x urllib.always_safe is private in Python 3.x; its content is copied here
    _ALWAYS_SAFE_BYTES = (b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                          b'abcdefghijklmnopqrstuvwxyz'
                          b'0123456789' b'_.-')

    _reserved = b';/?:@&=+$|,#'  # RFC 3986 (Generic Syntax)
    _unreserved_marks = b"-_.!~*'()"  # RFC 3986 sec 2.3
    _safe_chars = _ALWAYS_SAFE_BYTES + b'%' + _reserved + _unreserved_marks

    TRY_LIMIT = 2  # For code 429, only give it one extra try

    def __init__(self, noverify, cookiefile, notes_limit):
        self.notes_limit = notes_limit
        self.lasturl = None

        handlers = []  # type: List[BaseHandler]  # pytype: disable=invalid-annotation
        if HAVE_SSL_CTX:
            context = ssl._create_unverified_context() if noverify else ssl.create_default_context()
            handlers.append(HTTPSHandler(context=context))
        if cookiefile:
            cookies = MozillaCookieJar(cookiefile)
            cookies.load()

            # Session cookies are denoted by either `expires` field set to an empty string or 0. MozillaCookieJar only
            # recognizes the former (see https://bugs.python.org/issue17164).
            for cookie in cookies:
                if cookie.expires == 0:
                    cookie.expires = None
                    cookie.discard = True

            handlers.append(HTTPCookieProcessor(cookies))

        self.opener = build_opener(*handlers)

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

        return urlunsplit(tuple(itertools.chain(
            (to_native_str(parts.scheme), to_native_str(netloc).rstrip(':')),
            (cls.quote_unsafe(getattr(parts, p)) for p in ('path', 'query', 'fragment')),
        )))

    def ratelimit_sleep(self, headers):
        rlr = headers.get('X-Rate-Limit-Reset')
        if rlr is None:
            return False

        try:
            irlr = int(rlr)
        except ValueError:
            log(self.lasturl, "Expected integer X-Rate-Limit-Reset, got '{}'".format(rlr))
            return False

        now = time.time()
        if irlr < now:
            log(self.lasturl, 'Warning: X-Rate-Limit-Reset is {} seconds in the past'.format(now - irlr))
            return True

        sleep_dur = irlr - now
        if sleep_dur > 20 * 60:
            log(self.lasturl, 'Refusing to sleep for {} minutes, giving up'.format(round(sleep_dur / 60)))
            return False

        log(self.lasturl, 'Rate limited, sleeping for {} seconds as requested'.format(round(sleep_dur)))
        time.sleep(sleep_dur)
        return True

    def urlopen(self, iri):
        self.lasturl = iri
        uri = self.iri_to_uri(iri)
        try_count = 0
        while True:
            try:
                with contextlib.closing(self.opener.open(uri, timeout=HTTP_TIMEOUT)) as resp:
                    try_count += 1
                    parsed_uri = urlparse(resp.geturl())
                    if re.match(r'(www\.)?tumblr\.com', parsed_uri.netloc) and parsed_uri.path == '/safe-mode':
                        sys.exit(EXIT_SAFE_MODE)
                    return resp.read().decode('utf-8', errors='ignore')
            except HTTPError as e:
                if e.code == 429 and try_count < self.TRY_LIMIT and self.ratelimit_sleep(e.headers):
                    continue
                raise

    @staticmethod
    def get_more_link(soup, base, notes_url):
        global ident
        element = soup.find('a', class_='more_notes_link')
        if not element:
            return None
        onclick = element.get_attribute_list('onclick')[0]
        if not onclick:
            log(notes_url, 'No onclick attribute, probably a dashboard-only blog')
            return None
        match = re.search(r";tumblrReq\.open\('GET','([^']+)'", onclick)
        if not match:
            log(notes_url, 'tumblrReq regex failed, did Tumblr update?')
            return None
        path = match.group(1)
        if not path.startswith('/'):
            path = '/' + path
        return base + path

    @staticmethod
    def append_notes(soup, notes_list, notes_url):
        notes = soup.find('ol', class_='notes')
        if notes is None:
            log(notes_url, 'Response HTML does not have a notes list')
            return False
        notes = notes.find_all('li')
        for n in reversed(notes):
            if 'more_notes_link_container' not in n.get('class', []):
                notes_list.append(n.prettify())
        return True

    def get_notes(self, post_url):
        parsed_uri = urlparse(post_url)
        base = '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)

        notes_10k = 0
        notes_list = []  # type: List[Text]

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
                log(notes_url, 'Note: {} notes retrieved so far'.format(notes_10k * 10000))
            if self.notes_limit != 0 and len(notes_list) > self.notes_limit:
                log(notes_url, 'Warning: Reached notes limit, stopping early.')
                break

        return u''.join(notes_list)


if __name__ == '__main__':
    post_url, ident, noverify, notes_limit, cookiefile, msg_fd = sys.argv[1:]

    with io.open(int(msg_fd), 'w') as msg_pipe:
        crawler = WebCrawler(bool(int(noverify)), cookiefile, int(notes_limit))

        try:
            notes = crawler.get_notes(post_url)
        except KeyboardInterrupt:
            sys.exit()  # Ignore these so they don't propogate into the parent
        except HTTPError as e:
            log(crawler.lasturl, 'HTTP Error {} {}'.format(e.code, e.reason))
            sys.exit()
        except URLError as e:
            log(crawler.lasturl, 'URL Error: {}'.format(e.reason))
            sys.exit()
        except HTTPException as e:
            log(crawler.lasturl, 'HTTP Exception: {}'.format(e))
            sys.exit()
        except Exception:
            log(crawler.lasturl, 'Caught an exception')
            traceback.print_exc(file=msg_pipe)
            sys.exit()

    print(notes, end=u'')
