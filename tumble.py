#!/usr/bin/env python

"""Read a feed from stdin and post its entries to tumblr.

Options:
    -b sub-blog         Post to a sub-blog of your account.
    -e post-id          Edit the existing post with the given ID.
                        This only looks at the first entry of the feed.
    -d                  Debug mode: print the raw post data instead
                        of posting it to tumblr.com.
"""

"""Authorization is handled via OAuth. Prepare the file ~/.config/tumblr
with 5 lines:

    - your default blog name
    - the consumer key
    - the consumer secret
    - the access token
    - the access secret

You get these values by registering for a new application on the tumblr
developer site and running the included oauth.py with the consumer key and
secret as arguments to get an access token and secret.

Non-standard Python dependencies:
    - simplejson (http://pypi.python.org/pypi/simplejson/; for Python <= 2.6)
    - oauth2 (http://pypi.python.org/pypi/oauth2/)
    - httplib2 (http://pypi.python.org/pypi/httplib2/)
"""

import sys
import os
import getopt
import urllib
from datetime import datetime
from calendar import timegm
try:
    import simplejson as json   # for Python <= 2.6
except ImportError:
    import json

import oauth2 as oauth
import feedparser

URL_FMT = 'http://api.tumblr.com/v2/blog/%s/post'
CONFIG = '~/.config/tumblr'


class Tumble:

    def __init__(self, config_file):
        (
            self.blog,
            self.consumer_token, self.consumer_secret,
            self.access_token, self.access_secret
        ) = (s.strip() for s in open(config_file))
        self.post_id = None
        self.debug = False

    def tumble(self, feed):
        feed = feedparser.parse(feed)
        if self.post_id:
            return [self.post(feed.entries[0])]
        else:
            return [self.post(e) for e in feed.entries]

    def post(self, entry):
        # the first enclosure determines the media type
        enc = entry.get('enclosures', [])
        if enc:
            enc = enc[0]
        if enc and enc.type.startswith('image/'):
            data = {
                'type': 'photo', 'source': enc.href,
                'caption': entry.title, 'link': entry.link
            }
        elif enc and enc.type.startswith('audio/'):
            data = {
                'type': 'audio', 'caption': entry.title, 'external-url': enc.href
            }
        elif 'link' in entry and entry.link:
            data = {'type': 'link', 'url': entry.link, 'title': entry.title}
            if 'content' in entry:
                data['description'] = entry.content[0].value
            elif 'summary' in entry:
                data['description'] = entry.summary
        elif 'content' in entry:
            data = {'type': 'text', 'title': entry.title, 'body': entry.content[0].value}
        elif 'summary' in entry:
            data = {'type': 'text', 'title': entry.title, 'body': entry.summary}
        else:
            return 'unknown', entry
        if 'tags' in entry:
            data['tags'] = ','.join('"%s"' % t.term for t in entry.tags)
        for d in ('published_parsed', 'updated_parsed'):
            if d in entry:
                pub = datetime.fromtimestamp(timegm(entry.get(d)))
                data['date'] = pub.isoformat(' ')
                break

        if not '.' in self.blog:
            self.blog += '.tumblr.com'
        url = URL_FMT % self.blog
        if self.post_id:
            data['id'] = self.post_id
            op = 'edit'
            url += '/edit'
        else:
            op = 'post'
        if self.debug:
            return dict(url=url, entry=entry, data=data)

        for k in data:
            if type(data[k]) is unicode:
                data[k] = data[k].encode('utf-8')

        # do the OAuth thing
        consumer = oauth.Consumer(self.consumer_token, self.consumer_secret)
        token = oauth.Token(self.access_token, self.access_secret)
        client = oauth.Client(consumer, token)
        try:
            headers, resp = client.request(url, method='POST', body=urllib.urlencode(data))
            resp = json.loads(resp)
        except ValueError, e:
            return 'error', 'json', resp
        except Exception, e:
            return 'error', str(e)
        if resp['meta']['status'] in (200, 201):
            return op, str(resp['response']['id'])
        else:
            return 'error', headers, resp

if __name__ == '__main__':
    try:
        t = Tumble(os.path.expanduser(CONFIG))
    except:
        sys.stderr.write('Config file %s not found or not readable\n' % CONFIG);
        sys.exit(1)
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
            t.blog = v
        elif o == '-e':
            t.post_id = v
        elif o == '-d':
            t.debug = True
    result = t.tumble(sys.stdin)
    if result:
        import pprint
        pprint.pprint(result)
        if not t.debug and 'error' in [r[0] for r in result]:
            sys.exit(2)
    sys.exit(0)
