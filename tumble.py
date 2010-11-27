#!/usr/bin/env python

"""Read an Atom feed and post its entries to tumblr.com"""

import sys, urllib, urllib2, netrc
import xmltramp

HOST = 'www.tumblr.com'

def tumble(feed):
    auth = netrc.netrc().authenticators(HOST)
    if auth is not None:
	feed = xmltramp.parse(feed)
	return [post(auth, e) for e in feed['entry':]]

def post(auth, entry):
    content = entry['content']
    format = content().get('type', 'text')
    if format == 'xhtml':
	format = 'html'
	content = content[0]	# use the <div> element
    data = {
	'email': auth[0],
	'password': auth[2],
	'type': 'regular',
	'format': format,
	'title': str(entry['title']),
	'body': content.__repr__(1, 1),
    }
    return urllib2.urlopen('http://' + HOST + '/api/write', urllib.urlencode(data)).read()

if __name__ == '__main__':
    print tumble(sys.stdin.read())
