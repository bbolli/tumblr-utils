#!/usr/bin/env python
# encoding: utf-8

import cookielib
import os
import urllib2
import urlparse

import selenium
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from bs4 import BeautifulSoup

class WebCrawler:

    def __init__(self):
        self.gecko_driver = None

    def find_gecko_driver(self):
        for path in os.environ["PATH"].split(os.pathsep):
            try_loc = os.path.join(path, 'geckodriver')
            if os.access(try_loc, os.X_OK):
                self.gecko_driver = try_loc
            if os.name != 'nt':
                continue
            try_loc += '.exe'
            if os.access(try_loc, os.X_OK):
                self.gecko_driver = try_loc

        return self.gecko_driver

    def load(self, cookiefile):
        self.driver = None
        d_options = Options()
        d_options.set_headless(True)
        self.driver = webdriver.Firefox(options=d_options, executable_path=self.gecko_driver)

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

    def quit(self):
        self.driver.quit()
        self.driver = None

    def load_cookies(self):
        for cookie in self.cookies:
            # Setting domain to None automatically instructs most webdrivers to use the domain of the current window
            # handle
            cookie_dict = {'domain': None, 'name': cookie.name, 'value': cookie.value, 'secure': cookie.secure}
            if cookie.expires:
                cookie_dict['expiry'] = cookie.expires
            if cookie.path_specified:
                cookie_dict['path'] = cookie.path

            self.driver.add_cookie(cookie_dict)

    # Selenium
    def driver_get(self, url):
        self.driver.get(url)
        self.load_cookies()
        self.driver.get(url)

    # urllib2
    def urlopen(self, url):
        return self.opener.open(url)

    def get_html(self):
        return self.driver.execute_script("return document.documentElement.outerHTML")

    @staticmethod
    def get_more_link(soup, base):
        element = soup.find('a', class_='more_notes_link')
        if not element:
            return None
        onclick = element.get_attribute_list('onclick')[0]
        return base + re.search(r";tumblrReq\.open\('GET','([^']+)'", onclick).group(1)

    @staticmethod
    def append_notes(soup, list):
        notes = soup.find('ol', class_='notes')
        if notes is None:
            raise RuntimeError('Unexpected HTML, perhaps you need cookies?')
        notes = notes.find_all('li')[:-1]
        for n in reversed(notes):
            list.append(n.prettify())

    def get_notes(self, url):
        parsed_uri = urlparse.urlparse(url)
        base = '{uri.scheme}://{uri.netloc}/'.format(uri=parsed_uri)

        self.driver_get(url)
        html = self.get_html()
        soup = BeautifulSoup(html, 'lxml')

        notes_list = []
        self.append_notes(soup, notes_list)

        while True:
            more_link = self.get_more_link(soup, base)
            if not more_link:
                break
            with self.urlopen(more_link) as response:
                soup = BeautifulSoup(response, 'lxml')
            self.append_notes(soup, notes_list)

        return u''.join(notes_list)
