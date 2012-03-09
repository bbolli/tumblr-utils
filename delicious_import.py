#!/usr/bin/env python

"""Imports delicious.com links into Tumblr.

- get all your delicious.com links into a file:

    curl -u user:password \
        'https://api.del.icio.us/v1/posts/all?meta=yes' >links.xml

- if curl is missing:
  open the above URL in a browser and save the resulting page as "links.xml"
- run this script to post the links to Tumblr
- delete your delicious.com account

The link's timestamp, tags and privacy flag are kept.

XML format of a link:

<post description="The problem with Django's Template Tags"
    extended="My reply"
    hash="ec46dd48f9b117f2adc8d7b10b8356d7"
    href="http://ericholscher.com/blog/2008/nov/8/problem-django-template-tags/"
    meta="28717456b732b4117631ff27fd959708"
    private="yes" shared="no" tag="reply django templetetags"
    time="2008-12-04T07:33:25Z"
/>
"""

import sys
import os
import getopt
import urllib
import urllib2
import netrc
from time import strptime, sleep
from datetime import datetime
from calendar import timegm

import xmltramp

DEBUG = False

HOST = 'www.tumblr.com'

def tumble(links):
    auth = netrc.netrc().authenticators(HOST)
    if auth is not None:
        auth = {'email': auth[0], 'password': auth[2]}
        return [post(auth, e) for e in links]

def post(auth, link):
    ''
    data = {'type': 'link', 'url': link('href'), 'name': link('description')}
    if link('private') == 'yes':
        data['private'] = '1'
    ext = link('extended')
    if ext:
        data['description'] = ext
    tags = link('tag')
    if tags:
        data['tags'] = ','.join('"%s"' % t for t in tags.split())
    t = datetime.fromtimestamp(timegm(strptime(link('time'), '%Y-%m-%dT%H:%M:%SZ')))
    data['date'] = t.isoformat(' ')

    if DEBUG:
        return 'debug', data

    data.update(auth)
    for k in data:
        if type(data[k]) is unicode:
            data[k] = data[k].encode('utf-8')

    sleep(2)
    print data['url']
    try:
        return 'ok', urllib2.urlopen('http://' + HOST + '/api/write', urllib.urlencode(data)).read()
    except Exception, e:
        return 'error', repr(e)

if __name__ == '__main__':
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'd')
    except:
        print "Usage: %s [-d]" % sys.argv[0].split(os.sep)[-1]
        sys.exit(1)
    for o, v in opts:
        if o == '-d':
            DEBUG = True
    links = xmltramp.parse(open('links.xml').read())
    result = tumble(links)
    if result:
        import pprint
        pprint.pprint(result)
        if 'error' in [r[0] for r in result]:
            sys.exit(2)
    sys.exit(0)
