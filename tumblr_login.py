#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Credit to johanneszab for the C# implementation in TumblThree.
# Credit to MrEldritch for the initial Python port.
# Cleaned up and split off by Cebtenzzre.

"""
This script works in both Python 2 & 3.
It uses Tumblr's internal SVC API to access a hidden or explicit blog,
and retrieves a JSON of very similar (but not quite identical) format to the
normal API.
"""

import sys
from getpass import getpass

from bs4 import BeautifulSoup

try:
    from http.cookiejar import MozillaCookieJar
except ImportError:
    from cookielib import MozillaCookieJar  # type: ignore[no-redef]

try:
    import requests
except ImportError:
    # Import pip._internal.download first to avoid a potential recursive import
    try:
        from pip._internal import download as _  # noqa: F401
    except ImportError:
        pass  # Not absolutely necessary
    try:
        from pip._vendor import requests  # type: ignore[no-redef]
    except ImportError:
        raise RuntimeError('The requests module is required. Please install it with pip or your package manager.')

# This builtin has a new name in Python 3
try:
    raw_input  # type: ignore[has-type]
except NameError:
    raw_input = input


def get_tumblr_key():
    r = session.get('https://www.tumblr.com/login')
    if r.status_code != 200:
        raise ValueError('Response has non-200 status: HTTP {} {}'.format(r.status_code, r.reason))
    soup = BeautifulSoup(r.text, 'lxml')
    head, = soup.find_all('head')
    key_meta, = soup.find_all('meta', attrs={'name': 'tumblr-form-key'})
    return key_meta['content']


def tumblr_login(session, login, password):
    tumblr_key = get_tumblr_key()

    # You need to make these two requests in order to pick up the proper cookies
    # in order to access login-required blogs (both dash-only & explicit)

    common_headers = {
        'Authority': 'www.tumblr.com',
        'Referer': 'https://www.tumblr.com/login',
        'Origin': 'https://www.tumblr.com',
    }
    common_params = {
        'tumblelog[name]': '',
        'user[age]': '',
        'context': 'no_referer',
        'version': 'STANDARD',
        'follow': '',
        'form_key': tumblr_key,
        'seen_suggestion': '0',
        'used_suggestion': '0',
        'used_auto_suggestion': '0',
        'about_tumblr_slide': '',
        'random_username_suggestions': '["KawaiiBouquetStranger","KeenTravelerFury","RainyMakerTastemaker"'
                                       ',"SuperbEnthusiastCollective","TeenageYouthFestival"]',
        'action': 'signup_determine',
    }

    # Register
    headers = common_headers.copy()
    headers.update({
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
    })
    parameters = common_params.copy()
    parameters.update({
        'determine_email': login,
        'user[email]': '',
        'user[password]': '',
        'tracking_url': '/login',
        'tracking_version': 'modal',
    })
    r = session.post('https://www.tumblr.com/svc/account/register', data=parameters, headers=headers)
    if r.status_code != 200:
        raise ValueError('Response has non-200 status: HTTP {} {}'.format(r.status_code, r.reason))

    # Authenticate
    headers = common_headers.copy()
    headers.update({
        'Content-Type': 'application/x-www-form-urlencoded',
    })
    parameters = common_params.copy()
    parameters.update({
        'determine_email': login,
        'user[email]': login,
        'user[password]': password,
    })
    r = session.post('https://www.tumblr.com/login', data=parameters, headers=headers)
    if r.status_code != 200:
        raise ValueError('Response has non-200 status: HTTP {} {}'.format(r.status_code, r.reason))

    # We now have the necessary cookies loaded into our session.


if __name__ == '__main__':
    cookiefile, = sys.argv[1:]

    print('Enter the credentials for your Tumblr account.')
    login = raw_input('Email: ')
    password = getpass()

    # Create a requests session with cookies
    session = requests.Session()
    session.cookies = MozillaCookieJar(cookiefile)  # type: ignore[assignment]
    session.headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                     'AppleWebKit/537.36 (KHTML, like Gecko) '
                                     'Chrome/85.0.4183.121 '
                                     'Safari/537.36')

    # Log into Tumblr
    tumblr_login(session, login, password)

    # Save the cookies
    session.cookies.save(ignore_discard=True)  # type: ignore[attr-defined]
