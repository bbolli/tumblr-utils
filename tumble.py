#!/usr/bin/env python

"""Read a feed and post its entries to tumblr.com"""

import sys, urllib, urllib2, netrc
import feedparser

HOST = 'www.tumblr.com'

def tumble(feed):
    auth = netrc.netrc().authenticators(HOST)
    if auth is not None:
	feed = feedparser.parse(feed)
	return [post(auth, e) for e in feed.entries]

def post(auth, entry):
    content = entry.content[0]
    format = content.type.split('/')[1]
    format = 'html' if 'html' in format else 'text'
    data = {
	'email': auth[0],
	'password': auth[2],
	'type': 'regular',
	'format': format,
	'title': entry.title,
	'body': content.value,
    }
    return urllib2.urlopen('http://' + HOST + '/api/write', urllib.urlencode(data)).read()

if __name__ == '__main__':
    print tumble(sys.stdin)
