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

import re
import sys
from getpass import getpass

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


def get_api_token():
    r = session.get('https://www.tumblr.com/login')
    if r.status_code != 200:
        raise ValueError('Response has non-200 status: HTTP {} {}'.format(r.status_code, r.reason))
    # https://stackoverflow.com/a/1732454
    match = re.search(r'"API_TOKEN":"([^"]+)"', r.text)
    if not match:
        raise ValueError('Could not find API token in Tumblr response')
    return match.group(1)


def tumblr_login(session, login, password):
    api_token = get_api_token()

    headers = {
        'Authorization': 'Bearer {}'.format(api_token),
        'Origin': 'https://www.tumblr.com',
        'Referer': 'https://www.tumblr.com/login',
    }
    request_body = {
        'grant_type': 'password',
        'username': login,
        'password': password,
    }
    r = session.post('https://www.tumblr.com/api/v2/oauth2/token', headers=headers, json=request_body)
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
    session.headers['User-Agent'] = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.71 '
        'Safari/537.36'
    )

    # Log into Tumblr
    tumblr_login(session, login, password)

    # Save the cookies
    session.cookies.save(ignore_discard=True)  # type: ignore[attr-defined]
