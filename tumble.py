#!/usr/bin/env python3

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
    - feedparser (https://pypi.python.org/pypi/feedparser/)
    - oauth2 (https://pypi.python.org/pypi/oauth2/)
"""

import sys
from os.path import expanduser
import argparse
from urllib.parse import urlencode
from datetime import datetime
from calendar import timegm
import json

import oauth2 as oauth
import feedparser

URL_FMT = 'https://api.tumblr.com/v2/blog/%s/post'


class Tumble:

    def __init__(self, args):
        self.blog = self.consumer_token = self.consumer_secret = None
        self.access_token = self.access_secret = None
        self.args = args

    def set_credentials(self):
        (
            self.blog,
            self.consumer_token, self.consumer_secret,
            self.access_token, self.access_secret
        ) = (s.strip() for s in open(expanduser(self.args.cred_file)))

    def tumble(self, feed):
        if self.args.sub_blog:
            self.blog = self.args.sub_blog
        if '.' not in self.blog:
            self.blog += '.tumblr.com'

        feed = feedparser.parse(feed)
        if self.args.post_id:
            return [self.post(feed.entries[0], self.args.post_id)]
        else:
            return [self.post(e, None) for e in feed.entries]

    def post(self, entry, post_id):
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

        url = URL_FMT % self.blog
        if post_id:
            data['id'] = post_id
            op = 'edit'
            url += '/edit'
        else:
            op = 'post'
        if self.args.debug:
            return dict(url=url, entry=entry, data=data)

        for k in data:
            if type(data[k]) is str:
                data[k] = data[k].encode('utf-8')

        # do the OAuth thing
        consumer = oauth.Consumer(self.consumer_token, self.consumer_secret)
        token = oauth.Token(self.access_token, self.access_secret)
        client = oauth.Client(consumer, token)
        try:
            headers, resp = client.request(url, method='POST', body=urlencode(data))
            resp = json.loads(resp)
        except ValueError:
            return 'error', 'json', resp
        except EnvironmentError as e:
            return 'error', str(e)
        if resp['meta']['status'] in (200, 201):
            return op, str(resp['response']['id'])
        else:
            return 'error', headers, resp


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Read an RSS feed from stdin and post its entries to tumblr."
    )
    parser.add_argument(
        '-b', '--sub-blog', help="post to a sub-blog of your account"
    )
    parser.add_argument(
        '-c', '--cred-file', default='~/.config/tumblr',
        help="the name of the credentials file"
    )
    parser.add_argument(
        '-e', '--post-id', help="edit the existing post with the given ID "
        "using the feed's first entry"
    )
    parser.add_argument(
        '-d', '--debug', action='store_true',
        help="debug mode: print the raw post data instead of posting it"
    )
    args = parser.parse_args()

    t = Tumble(args)
    try:
        t.set_credentials()
    except EnvironmentError:
        print(f'Credentials file {args.cred_file} not found or not readable\n', file=sys.stderr)
        sys.exit(1)
    result = t.tumble(sys.stdin.buffer)  # read stdin in binary mode
    if result:
        import pprint
        pprint.pprint(result)
        if not args.debug and 'error' in [r[0] for r in result]:
            sys.exit(2)
    sys.exit(0)
