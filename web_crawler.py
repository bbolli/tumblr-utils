#!/usr/bin/env python
# encoding: utf-8

import contextlib
import cookielib
import re
import urllib2
import urlparse

# This is optional for tumblr_backup
try:
    import bs4
    from bs4 import BeautifulSoup
except ImportError:
    bs4 = None

class WebCrawler:

    def __init__(self, cookiefile):
        self.lasturl = None

        if cookiefile:
            self.cookies = cookielib.MozillaCookieJar(cookiefile)
            self.cookies.load(ignore_discard=False, ignore_expires=False)

            # Session cookies are denoted by either `expires` field set to
            # an empty string or 0. MozillaCookieJar only recognizes the former
            # (see [1]). So we need force the latter to be recognized as session
            # cookies on our own.
            # Session cookies may be important for cookies-based authentication,
            # e.g. usually, when user does not check 'Remember me' check box while
            # logging in on a site, some important cookies are stored as session
            # cookies so that not recognizing them will result in failed login.
            # 1. https://bugs.python.org/issue17164
            for cookie in self.cookies:
                # Treat `expires=0` cookies as session cookies
                if cookie.expires == 0:
                    cookie.expires = None
                    cookie.discard = True

            cookie_handler = urllib2.HTTPCookieProcessor(self.cookies)
            redirect_handler = urllib2.HTTPRedirectHandler()
            self.opener = urllib2.build_opener(cookie_handler, redirect_handler)
        else:
            self.opener = urllib2.build_opener()

    @staticmethod
    def url_encode(b):
        return re.sub('[\x80-\xFF]', lambda c: '%%%02x' % ord(c.group(0)), b)

    @staticmethod
    def iri_to_uri(iri):
        parts = urlparse.urlparse(iri)
        return urlparse.urlunparse(
            part.encode('idna') if parti == 1 else WebCrawler.url_encode(part.encode('utf-8'))
            for parti, part in enumerate(parts)
        )

    # urllib2
    def urlopen(self, url):
        self.lasturl = url
        return self.opener.open(self.iri_to_uri(url))

    @staticmethod
    def get_more_link(soup, base):
        element = soup.find('a', class_='more_notes_link')
        if not element:
            return None
        onclick = element.get_attribute_list('onclick')[0]
        path = re.search(r";tumblrReq\.open\('GET','([^']+)'", onclick).group(1)
        if not path.startswith('/'):
            path = '/' + path
        return base + path

    @staticmethod
    def append_notes(soup, list):
        notes = soup.find('ol', class_='notes')
        if notes is None:
            raise RuntimeError('Unexpected HTML, perhaps you need cookies?')
        notes = notes.find_all('li')[:-1]
        for n in reversed(notes):
            list.append(n.prettify())

    def get_notes(self, post_url):
        parsed_uri = urlparse.urlparse(post_url)
        base = '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)

        notes_list = []

        old_notes_url = None
        notes_url = post_url
        while True:
            with contextlib.closing(self.urlopen(notes_url)) as response:
                soup = BeautifulSoup(response.read().decode('utf-8', 'ignore'), 'lxml')
            self.append_notes(soup, notes_list)

            notes_url = self.get_more_link(soup, base)
            if (not notes_url) or notes_url == old_notes_url:
                break

        return u''.join(notes_list)
