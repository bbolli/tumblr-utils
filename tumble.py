#!/usr/bin/env python

"""Read a feed and post its entries to tumblr.com"""

import sys, urllib, urllib2, netrc, datetime, calendar
import feedparser

HOST = 'www.tumblr.com'

def tumble(feed):
    auth = netrc.netrc().authenticators(HOST)
    if auth is not None:
	auth = {'email': auth[0], 'password': auth[2]}
	feed = feedparser.parse(feed)
	return [post(auth, e) for e in feed.entries]

def post(auth, entry):
    enc = entry.get('enclosures', [None])[0]
    if enc and enc.type.startswith('image/'):
	data = {
	    'type': 'photo', 'source': enc.href,
	    'caption': entry.title, 'click-through-url': entry.link
	}
    elif enc and enc.type.startswith('audio/'):
	data = {
	    'type': 'audio', 'caption': entry.title, 'externally-hosted-url': enc.href
	}
    else:
	content = entry.content[0]
	data = {
	    'type': 'regular', 'title': entry.title, 'body': content.value,
	    'format': 'html' if 'html' in content.type.split('/')[1] else 'text'
	}
    if 'tags' in entry:
	data['tags'] = ','.join('"%s"' % t.term for t in entry.tags)
    pub = datetime.datetime.fromtimestamp(calendar.timegm(entry.published_parsed))
    data['date'] = pub.isoformat(' ')
    data.update(auth)
    for k in data:
	if type(data[k]) == unicode:
	    data[k] = data[k].encode('utf-8')
    return data
    return urllib2.urlopen('http://' + HOST + '/api/write', urllib.urlencode(data)).read()

if __name__ == '__main__':
    print tumble(sys.stdin)
