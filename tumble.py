#!/usr/bin/env python

"""Read a feed from stdin and post its entries to tumblr.

User name and password are read from your ~/.netrc entry for
machine www.tumblr.com.

Options:
    -b sub-blog         Post to a sub-blog of your account.
    -e post-id          Edit the existing post with the given ID.
                        This only looks at the first entry of the feed.
    -d                  Debug mode: print the raw post data instead
                        of posting it to tumblr.com.
"""

import sys, os, getopt, urllib, urllib2, netrc
import feedparser

from datetime import datetime
from calendar import timegm

BLOG = None             # or a sub-blog of your account
POST = None             # or the post-id of a post to edit
DEBUG = False

HOST = 'www.tumblr.com'

def tumble(feed):
    auth = netrc.netrc().authenticators(HOST)
    if auth is not None:
        auth = {'email': auth[0], 'password': auth[2]}
        feed = feedparser.parse(feed)
        if POST:
            return [post(auth, feed.entries[0])]
        else:
            return [post(auth, e) for e in feed.entries]

def post(auth, entry):
    enc = entry.get('enclosures', [])
    if enc: enc = enc[0]
    if enc and enc.type.startswith('image/'):
        data = {
            'type': 'photo', 'source': enc.href,
            'caption': entry.title, 'click-through-url': entry.link
        }
    elif enc and enc.type.startswith('audio/'):
        data = {
            'type': 'audio', 'caption': entry.title, 'externally-hosted-url': enc.href
        }
    elif enc and enc.type.startswith('video/'):
        data = {
            'type': 'video', 'caption': entry.title, 'embed': enc.href
        }
    elif 'link' in entry:
        data = {'type': 'link', 'url': entry.link, 'name': entry.title}
        if 'content' in entry:
            data['description'] = entry.content[0].value
        elif 'summary' in entry:
            data['description'] = entry.summary
    elif 'content' in entry:
        data = {'type': 'regular', 'title': entry.title, 'body': entry.content[0].value}
    elif 'summary' in entry:
        data = {'type': 'regular', 'title': entry.title, 'body': entry.summary}
    else:
        return 'unknown', entry
    if 'tags' in entry:
        data['tags'] = ','.join('"%s"' % t.term for t in entry.tags)
    for d in ('published_parsed', 'updated_parsed'):
        if d in entry:
            pub = datetime.fromtimestamp(timegm(entry.get(d)))
            data['date'] = pub.isoformat(' ')
            break
    if BLOG:
        data['group'] = BLOG
    if POST:
        data['post-id'] = POST
    if DEBUG:
        return 'debug', entry.get('id'), data

    data.update(auth)
    for k in data:
        if type(data[k]) is unicode:
            data[k] = data[k].encode('utf-8')

    try:
        return 'ok', urllib2.urlopen('http://' + HOST + '/api/write', urllib.urlencode(data)).read()
    except Exception, e:
        return 'error', str(e)

if __name__ == '__main__':
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hb:e:d')
    except:
        print "Usage: %s [-b blog-name] [-e post-id] [-d]" % sys.argv[0].split(os.sep)[-1]
        sys.exit(1)
    for o, v in opts:
        if o == '-h':
            print __doc__.strip()
            sys.exit(0)
        if o == '-b':
            BLOG = v
        elif o == '-e':
            POST = v
        elif o == '-d':
            DEBUG = True
    result = tumble(sys.stdin)
    if result:
        import pprint
        pprint.pprint(result)
        if 'error' in [r[0] for r in result]:
            sys.exit(2)
    sys.exit(0)
