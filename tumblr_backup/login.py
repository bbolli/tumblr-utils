# Credit to johanneszab for the C# implementation in TumblThree.
# Credit to MrEldritch for the initial Python port.
# Cleaned up and split off by Cebtenzzre.

"""
This script uses Tumblr's internal SVC API to access a hidden or explicit blog,
and retrieves a JSON of very similar (but not quite identical) format to the
normal API.
"""

from __future__ import annotations

import re
import sys
from getpass import getpass
from http.cookiejar import MozillaCookieJar

import requests


def get_api_token(session):
    r = session.get('https://www.tumblr.com/login')
    if r.status_code != 200:
        raise ValueError('Response has non-200 status: HTTP {} {}'.format(r.status_code, r.reason))
    # https://stackoverflow.com/a/1732454
    match = re.search(r'"API_TOKEN":"([^"]+)"', r.text)
    if not match:
        raise ValueError('Could not find API token in Tumblr response')
    return match.group(1)


def tumblr_login(session, login, password):
    api_token = get_api_token(session)

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


def main():
    cookiefile, = sys.argv[1:]

    print('Enter the credentials for your Tumblr account.')
    login = input('Email: ')
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
