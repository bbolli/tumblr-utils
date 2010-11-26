#!/usr/bin/env python

"""Read an Atom feed and post its entries to tumblr.com"""

import sys, urllib, urllib2, netrc


def tumble(feed):
    host = 'www.tumblr.com'
    auth = netrc.netrc().authenticators(host)
    if auth is not None:
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
    return urllib2.urlopen('http://' + host + '/api/write', urllib.urlencode(data)).read()

if __name__ == '__main__':
    import xmltramp
    print tumble(xmltramp.parse(sys.stdin.read()))
