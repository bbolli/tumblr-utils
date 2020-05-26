#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

# standard Python library imports
import errno
import hashlib
import imghdr
import io
import locale
import os
import re
import ssl
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from glob import glob
from os.path import join, split, splitext
from xml.sax.saxutils import escape

from util import to_bytes, to_unicode

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from queue import Queue
    from typing import Any, Callable, DefaultDict, Dict, List, Optional, Set, Text, Tuple, Type

    JSONDict = Dict[str, Any]

try:
    from http.client import HTTPException
except ImportError:
    from httplib import HTTPException  # type: ignore[no-redef]

try:
    import json
except ImportError:
    import simplejson as json  # type: ignore[no-redef]

try:
    import queue
except ImportError:
    import Queue as queue  # type: ignore[no-redef]

try:
    from urllib.parse import quote, urlencode, urlparse
    from urllib.request import urlopen
    NEW_URLLIB = True
except ImportError:
    from urllib import quote, urlencode  # type: ignore[attr-defined,no-redef]
    from urllib2 import urlopen  # type: ignore[no-redef]
    from urlparse import urlparse  # type: ignore[no-redef]
    NEW_URLLIB = False

try:
    from settings import DEFAULT_BLOGS
except ImportError:
    DEFAULT_BLOGS = []

# extra optional packages
try:
    import pyexiv2
except ImportError:
    pyexiv2 = None

try:
    import youtube_dl
    from youtube_dl.utils import sanitize_filename
except ImportError:
    youtube_dl = None

# These builtins have new names in Python 3
try:
    long, xrange  # type: ignore[has-type]
except NameError:
    long = int
    xrange = range

# Format of displayed tags
TAG_FMT = u'#{}'

# Format of tag link URLs; set to None to suppress the links.
# Named placeholders that will be replaced: domain, tag
TAGLINK_FMT = u'https://{domain}/tagged/{tag}'

# exit codes
EXIT_SUCCESS    = 0
EXIT_NOPOSTS    = 1
# EXIT_ARGPARSE = 2 -- returned by argparse
EXIT_INTERRUPT  = 3
EXIT_ERRORS     = 4

# add another JPEG recognizer
# see http://www.garykessler.net/library/file_sigs.html
def test_jpg(h, f):
    if h[:3] == b'\xFF\xD8\xFF' and h[3] in b'\xDB\xE0\xE1\xE2\xE3':
        return 'jpg'

imghdr.tests.append(test_jpg)

# variable directory names, will be set in TumblrBackup.backup()
save_folder = ''
media_folder = ''

# constant names
root_folder = os.getcwd()
post_dir = 'posts'
json_dir = 'json'
media_dir = 'media'
archive_dir = 'archive'
theme_dir = 'theme'
save_dir = '../'
backup_css = 'backup.css'
custom_css = 'custom.css'
avatar_base = 'avatar'
dir_index = 'index.html'
tag_index_dir = 'tags'

blog_name = ''
post_ext = '.html'
have_custom_css = False

POST_TYPES = ('text', 'quote', 'link', 'answer', 'video', 'audio', 'photo', 'chat')
TYPE_ANY = 'any'
TAG_ANY = '__all__'

MAX_POSTS = 50

HTTP_TIMEOUT = 90
HTTP_CHUNK_SIZE = 1024 * 1024

# get your own API key at https://www.tumblr.com/oauth/apps
API_KEY = ''

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass
FILE_ENCODING = 'utf-8'
TIME_ENCODING = locale.getlocale(locale.LC_TIME)[1] or FILE_ENCODING


have_ssl_ctx = sys.version_info >= (2, 7, 9)
if have_ssl_ctx:
    ssl_ctx = ssl.create_default_context()
    def tb_urlopen(url):
        return urlopen(url, timeout=HTTP_TIMEOUT, context=ssl_ctx)
else:
    def tb_urlopen(url):
        return urlopen(url, timeout=HTTP_TIMEOUT)


def log(msg, account=None):
    if options.quiet:
        return

    # Separate terminator
    it = (i for i, c in enumerate(reversed(msg)) if c not in '\r\n')
    try:
        idx = len(msg) - next(it)
    except StopIteration:
        idx = 0
    msg, term = msg[:idx], msg[idx:]

    if account is not None:  # Optional account prefix
        msg = '{}: {}'.format(account, msg)
    msg += ''.join([' ' for _ in range(80 - len(msg))])  # Pad to 80 chars
    print(msg + term, end='')
    sys.stdout.flush()


def mkdir(dir, recursive=False):
    if not os.path.exists(dir):
        try:
            if recursive:
                os.makedirs(dir)
            else:
                os.mkdir(dir)
        except EnvironmentError as e:
            if getattr(e, 'errno', None) != errno.EEXIST:
                raise


def path_to(*parts):
    return join(save_folder, *parts)


def open_file(open_fn, parts):
    if len(parts) > 1:
        mkdir(path_to(*parts[:-1]), (len(parts) > 2))
    return open_fn(path_to(*parts))


def open_text(*parts):
    return open_file(
        lambda f: io.open(f, 'w', encoding=FILE_ENCODING, errors='xmlcharrefreplace'), parts
    )


def open_media(*parts):
    return open_file(lambda f: io.open(f, 'wb'), parts)


def strftime(fmt, t=None):
    if t is None:
        t = time.localtime()
    s = time.strftime(fmt, t)
    return to_unicode(s, encoding=TIME_ENCODING)


def get_api_url(account):
    """construct the tumblr API URL"""
    global blog_name
    blog_name = account
    if '.' not in account:
        blog_name += '.tumblr.com'
    return 'https://api.tumblr.com/v2/blog/%s/%s' % (
        blog_name, 'likes' if options.likes else 'posts'
    )


def set_period():
    """Prepare the period start and end timestamps"""
    i = 0
    tm = [int(options.period[:4]), 1, 1, 0, 0, 0, 0, 0, -1]
    if len(options.period) >= 6:
        i = 1
        tm[1] = int(options.period[4:6])
    if len(options.period) == 8:
        i = 2
        tm[2] = int(options.period[6:8])

    def mktime(tml):
        tmt = tuple(tml)  # type: Any
        return time.mktime(tmt)

    options.p_start = mktime(tm)
    tm[i] += 1
    options.p_stop = mktime(tm)


def apiparse(base, count, start=0):
    params = {'api_key': API_KEY, 'limit': count, 'reblog_info': 'true'}
    if start > 0:
        params['offset'] = start
    url = base + '?' + urlencode(params)
    for _ in range(10):
        try:
            resp = tb_urlopen(url)
            data = resp.read()
        except (EnvironmentError, HTTPException) as e:
            log('URL is {}\n'.format(url))
            log('Error retrieving API repsonse: {}\n'.format(e))
            continue
        info = resp.info()
        if (info.get_content_type() if NEW_URLLIB else info.gettype()) == 'application/json':
            break
        log("Unexpected Content-Type: '{}'\n".format(resp.info().gettype()))
        return None
    else:
        return None
    try:
        doc = json.loads(data)
    except ValueError as e:
        log('{}: {}\n{} {} {}\n{!r}\n'.format(
            e.__class__.__name__, e, resp.getcode(), resp.msg, resp.info().gettype(), data,
        ))
        return None
    status = doc['meta']['status']
    if status != 200:
        log('API response has non-200 status:\n{}\n'.format(doc))
        return None
    return doc.get('response')


def add_exif(image_name, tags):
    try:
        metadata = pyexiv2.ImageMetadata(image_name)
        metadata.read()
    except EnvironmentError:
        log('Error reading metadata for image {}\n'.format(image_name))
        return
    KW_KEY = 'Iptc.Application2.Keywords'
    if '-' in options.exif:  # remove all tags
        if KW_KEY in metadata.iptc_keys:
            del metadata[KW_KEY]
    else:  # add tags
        if KW_KEY in metadata.iptc_keys:
            tags |= set(metadata[KW_KEY].value)
        tags = [tag.strip().lower() for tag in tags | options.exif if tag]
        metadata[KW_KEY] = pyexiv2.IptcTag(KW_KEY, tags)
    try:
        metadata.write()
    except EnvironmentError:
        log('Writing metadata failed for tags: {} in: {}\n'.format(tags, image_name))


def save_style():
    with open_text(backup_css) as css:
        css.write(u'''\
@import url("override.css");

body { width: 720px; margin: 0 auto; }
body > footer { padding: 1em 0; }
header > img { float: right; }
img { max-width: 720px; }
blockquote { margin-left: 0; border-left: 8px #999 solid; padding: 0 24px; }
.archive h1, .subtitle, article { padding-bottom: 0.75em; border-bottom: 1px #ccc dotted; }
article[class^="liked-"] { background-color: #f0f0f8; }
.post a.llink { display: none; }
header a, footer a { text-decoration: none; }
footer, article footer a { font-size: small; color: #999; }
''')


def get_avatar():
    try:
        resp = tb_urlopen('https://api.tumblr.com/v2/blog/%s/avatar' % blog_name)
        avatar_data = resp.read()
    except (EnvironmentError, HTTPException):
        return
    avatar_file = avatar_base
    ext = imghdr.what(None, avatar_data[:32])
    if ext is not None:
        avatar_file += '.' + ext
    with open_media(theme_dir, avatar_file) as f:
        f.write(avatar_data)


def get_style():
    """Get the blog's CSS by brute-forcing it from the home page.
    The v2 API has no method for getting the style directly.
    See https://groups.google.com/d/msg/tumblr-api/f-rRH6gOb6w/sAXZIeYx5AUJ"""
    try:
        resp = tb_urlopen('https://%s/' % blog_name)
        page_data = resp.read()
    except (EnvironmentError, HTTPException):
        return
    for match in re.findall(br'(?s)<style type=.text/css.>(.*?)</style>', page_data):
        css = match.strip().decode('utf-8', errors='replace')
        if '\n' not in css:
            continue
        css = css.replace('\r', '').replace('\n    ', '\n')
        with open_text(theme_dir, 'style.css') as f:
            f.write(css + '\n')
        return


class Index(object):
    def __init__(self, blog, body_class='index'):
        self.blog = blog
        self.body_class = body_class
        self.index = defaultdict(lambda: defaultdict(list))  # type: DefaultDict[int, DefaultDict[int, List[LocalPost]]]

    def add_post(self, post):
        self.index[post.tm.tm_year][post.tm.tm_mon].append(post)

    def save_index(self, index_dir='.', title=None):
        archives = sorted(((y, m) for y in self.index for m in self.index[y]),
            reverse=options.reverse_month
        )
        subtitle = self.blog.title if title else self.blog.subtitle
        title = title or self.blog.title
        with open_text(index_dir, dir_index) as idx:
            idx.write(self.blog.header(title, self.body_class, subtitle, avatar=True))
            if options.tag_index and self.body_class == 'index':
                idx.write('<p><a href=%s/%s>Tag index</a></p>\n' % (
                    tag_index_dir, dir_index
                ))
            for year in sorted(self.index.keys(), reverse=options.reverse_index):
                self.save_year(idx, archives, index_dir, year)
            idx.write(u'<footer><p>Generated on %s by <a href=https://github.com/'
                'bbolli/tumblr-utils>tumblr-utils</a>.</p></footer>\n' % strftime('%x %X')
            )

    def save_year(self, idx, archives, index_dir, year):
        idx.write(u'<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=options.reverse_index):
            tm = time.localtime(time.mktime((year, month, 3, 0, 0, 0, 0, 0, -1)))
            month_name = self.save_month(archives, index_dir, year, month, tm)
            idx.write(u'    <li><a href=%s/%s title="%d post(s)">%s</a></li>\n' % (
                archive_dir, month_name, len(self.index[year][month]),
                strftime('%B', tm)
            ))
        idx.write(u'</ul>\n\n')

    def save_month(self, archives, index_dir, year, month, tm):
        posts = sorted(self.index[year][month], key=lambda x: x.date, reverse=options.reverse_month)
        posts_month = len(posts)
        posts_page = options.posts_per_page if options.posts_per_page >= 1 else posts_month

        def pages_per_month(y, m):
            posts_m = len(self.index[y][m])
            return posts_m // posts_page + bool(posts_m % posts_page)

        def next_month(inc):
            i = archives.index((year, month))
            i += inc
            if 0 <= i < len(archives):
                return archives[i]
            return 0, 0

        FILE_FMT = '%d-%02d-p%s'
        pages_month = pages_per_month(year, month)
        first_file = None  # type: Optional[str]
        for page, start in enumerate(xrange(0, posts_month, posts_page), start=1):

            archive = [self.blog.header(strftime('%B %Y', tm), body_class='archive')]
            archive.extend(p.get_post(self.body_class == 'tag-archive') for p in posts[start:start + posts_page])

            file_name = FILE_FMT % (year, month, page)
            if options.dirs:
                base = save_dir + archive_dir + '/'
                suffix = '/'
                arch = open_text(index_dir, archive_dir, file_name, dir_index)
                file_name += suffix
            else:
                base = ''
                suffix = post_ext
                file_name += suffix
                arch = open_text(index_dir, archive_dir, file_name)

            if page > 1:
                pp = FILE_FMT % (year, month, page - 1)
            else:
                py, pm = next_month(-1)
                pp = FILE_FMT % (py, pm, pages_per_month(py, pm)) if py else ''
                first_file = file_name

            if page < pages_month:
                np = FILE_FMT % (year, month, page + 1)
            else:
                ny, nm = next_month(+1)
                np = FILE_FMT % (ny, nm, 1) if ny else ''

            archive.append(self.blog.footer(base, pp, np, suffix))

            arch.write('\n'.join(archive))

        assert first_file is not None
        return first_file


class TagIndex(Index):
    def __init__(self, blog, name):
        super(TagIndex, self).__init__(blog, 'tag-archive')
        self.name = name


class Indices(object):
    def __init__(self, blog):
        self.blog = blog
        self.main_index = Index(blog)
        self.tags = {}

    def build_index(self):
        filter_ = join('*', dir_index) if options.dirs else '*' + post_ext
        for post in (LocalPost(f) for f in glob(path_to(post_dir, filter_))):
            self.main_index.add_post(post)
            if options.tag_index:
                for tag, name in post.tags:
                    if tag not in self.tags:
                        self.tags[tag] = TagIndex(self.blog, name)
                    self.tags[tag].name = name
                    self.tags[tag].add_post(post)

    def save_index(self):
        self.main_index.save_index()
        if options.tag_index:
            self.save_tag_index()

    def save_tag_index(self):
        global save_dir
        save_dir = '../../../'
        mkdir(path_to(tag_index_dir))
        tag_index = [self.blog.header('Tag index', 'tag-index', self.blog.title, avatar=True), '<ul>']
        for tag, index in sorted(self.tags.items(), key=lambda kv: kv[1].name):
            digest = hashlib.md5(to_bytes(tag)).hexdigest()
            index.save_index(tag_index_dir + os.sep + digest,
                u"Tag ‛%s’" % index.name
            )
            tag_index.append(u'    <li><a href=%s/%s>%s</a></li>' % (
                digest, dir_index, escape(index.name)
            ))
        tag_index.extend(['</ul>', ''])
        with open_text(tag_index_dir, dir_index) as f:
            f.write(u'\n'.join(tag_index))


class TumblrBackup(object):
    def __init__(self):
        self.errors = False
        self.total_count = 0
        self.post_count = 0
        self.title = None  # type: Optional[Text]
        self.subtitle = None  # type: Optional[str]

    def exit_code(self):
        if self.errors:
            return EXIT_ERRORS
        if self.total_count == 0:
            return EXIT_NOPOSTS
        return EXIT_SUCCESS

    def header(self, title='', body_class='', subtitle='', avatar=False):
        root_rel = {
            'index': '', 'tag-index': '../', 'tag-archive': '../../'
        }.get(body_class, save_dir)
        css_rel = root_rel + (custom_css if have_custom_css else backup_css)
        if body_class:
            body_class = ' class=' + body_class
        h = u'''<!DOCTYPE html>

<meta charset=%s>
<title>%s</title>
<link rel=stylesheet href=%s>

<body%s>

<header>
''' % (FILE_ENCODING, self.title, css_rel, body_class)
        if avatar:
            f = glob(path_to(theme_dir, avatar_base + '.*'))
            if f:
                h += '<img src=%s%s/%s alt=Avatar>\n' % (root_rel, theme_dir, split(f[0])[1])
        if title:
            h += u'<h1>%s</h1>\n' % title
        if subtitle:
            h += u'<p class=subtitle>%s</p>\n' % subtitle
        h += '</header>\n'
        return h

    @staticmethod
    def footer(base, previous_page, next_page, suffix):
        f = '<footer><nav>'
        f += '<a href=%s%s rel=index>Index</a>\n' % (save_dir, dir_index)
        if previous_page:
            f += '| <a href=%s%s%s rel=prev>Previous</a>\n' % (base, previous_page, suffix)
        if next_page:
            f += '| <a href=%s%s%s rel=next>Next</a>\n' % (base, next_page, suffix)
        f += '</nav></footer>\n'
        return f

    def backup(self, account):
        """makes single files and an index for every post on a public Tumblr blog account"""

        base = get_api_url(account)

        # make sure there are folders to save in
        global save_folder, media_folder, post_ext, post_dir, save_dir, have_custom_css
        if options.blosxom:
            save_folder = root_folder
            post_ext = '.txt'
            post_dir = os.curdir
            post_class = BlosxomPost  # type: Type[TumblrPost]
        else:
            save_folder = join(root_folder, options.outdir or account)
            media_folder = path_to(media_dir)
            if options.dirs:
                post_ext = ''
                save_dir = '../../'
                mkdir(path_to(post_dir), recursive=True)
            else:
                mkdir(save_folder, recursive=True)
            post_class = TumblrPost
            have_custom_css = os.access(path_to(custom_css), os.R_OK)

        self.post_count = 0

        # get the highest post id already saved
        ident_max = None
        if options.incremental:
            try:
                ident_max = max(
                    long(splitext(split(f)[1])[0])
                    for f in glob(path_to(post_dir, '*' + post_ext))
                )
                log('Backing up posts after {}\r'.format(ident_max), account)
            except ValueError:  # max() arg is an empty sequence
                pass
        else:
            log('Getting basic information\r', account)

        # start by calling the API with just a single post
        resp = apiparse(base, 1)
        if not resp:
            self.errors = True
            return

        # collect all the meta information
        if options.likes:
            posts_key = 'liked_posts'
            blog = {}
            count_estimate = resp['liked_count']
        else:
            posts_key = 'posts'
            blog = resp['blog']
            count_estimate = blog['posts']
        assert isinstance(count_estimate, int)
        self.title = escape(blog.get('title', account))
        self.subtitle = blog.get('description', '')

        # use the meta information to create a HTML header
        TumblrPost.post_header = self.header(body_class='post')

        # start the thread pool
        backup_pool = ThreadPool(account)

        # returns whether any posts from this batch were saved
        def _backup(posts):
            for p in sorted(posts, key=lambda x: x['id'], reverse=True):
                post = post_class(p, account)
                if ident_max and long(post.ident) <= ident_max:
                    return False
                if options.count and self.post_count >= options.count:
                    return False
                if options.period:
                    if post.date >= options.p_stop:
                        continue
                    if post.date < options.p_start:
                        return False
                if options.request:
                    if post.typ not in options.request:
                        continue
                    tags = options.request[post.typ]
                    if not (TAG_ANY in tags or tags & post.tags_lower):
                        continue
                if options.no_reblog:
                    if 'reblogged_from_name' in p or 'reblogged_root_name' in p:
                        if 'trail' in p and not p['trail']:
                            continue
                        if 'trail' in p and 'is_current_item' not in p['trail'][-1]:
                            continue
                    elif 'trail' in p and p['trail'] and 'is_current_item' not in p['trail'][-1]:
                        continue
                backup_pool.add_work(post.save_content)
                self.post_count += 1
            return True

        try:
            # Get the JSON entries from the API, which we can only do for MAX_POSTS posts at once.
            # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
            i = options.skip
            while True:
                # find the upper bound
                log('Getting posts {} to {} (of {} expected)\r'.format(i, i + MAX_POSTS - 1, count_estimate), account)

                resp = apiparse(base, MAX_POSTS, i)
                if resp is None:
                    i += 1 # try skipping a post
                    self.errors = True
                    continue

                posts = resp[posts_key]
                # `_backup(posts)` can be empty even when `posts` is not if we don't backup reblogged posts
                if not posts or not _backup(posts):
                    log('Backup complete: Found empty set of posts\n', account)
                    break

                i += MAX_POSTS
        except:
            # ensure proper thread pool termination
            backup_pool.cancel()
            raise

        # wait until all posts have been saved
        backup_pool.wait()

        # postprocessing
        if not options.blosxom and (self.post_count or options.count == 0):
            log('Getting avatar and style\r', account)
            get_avatar()
            get_style()
            if not have_custom_css:
                save_style()
            log('Building index\r', account)
            ix = Indices(self)
            ix.build_index()
            ix.save_index()

        log('{} posts backed up\n'.format(self.post_count), account)
        self.total_count += self.post_count


class TumblrPost(object):
    post_header = ''  # set by TumblrBackup.backup()

    def __init__(self, post, backup_account):
        # type: (JSONDict, str) -> None
        self.content = ''
        self.post = post
        self.backup_account = backup_account
        self.json_content = json.dumps(post, sort_keys=True, indent=4, separators=(',', ': '))
        self.creator = post['blog_name']
        self.ident = str(post['id'])
        self.url = post['post_url']
        self.shorturl = post['short_url']
        self.typ = str(post['type'])
        self.date = post['timestamp']
        self.isodate = datetime.utcfromtimestamp(self.date).isoformat() + 'Z'
        self.tm = time.localtime(self.date)
        self.title = u''
        self.tags = post['tags']
        self.note_count = post.get('note_count', 0)
        self.reblogged_from = post.get('reblogged_from_url')
        self.reblogged_root = post.get('reblogged_root_url')
        self.source_title = post.get('source_title', '')
        self.source_url = post.get('source_url', '')
        self.tags_lower = None  # type: Optional[Set[str]]
        if options.request:
            self.tags_lower = {t.lower() for t in self.tags}
        self.file_name = join(self.ident, dir_index) if options.dirs else self.ident + post_ext
        self.llink = self.ident if options.dirs else self.file_name
        self.media_dir = join(post_dir, self.ident) if options.dirs else media_dir
        self.media_url = save_dir + self.media_dir
        self.media_folder = path_to(self.media_dir)

    def save_content(self):
        """generates the content for this post"""
        post = self.post
        content = []

        def append(s, fmt=u'%s'):
            content.append(fmt % s)

        def get_try(elt):
            return post.get(elt, '')

        def append_try(elt, fmt=u'%s'):
            elt = get_try(elt)
            if elt:
                if options.save_images:
                    elt = re.sub(r'''(?i)(<img\s(?:[^>]*\s)?src\s*=\s*["'])(.*?)(["'][^>]*>)''',
                        self.get_inline_image, elt
                    )
                if options.save_video or options.save_video_tumblr:
                    # Handle video element poster attribute
                    elt = re.sub(r'''(?i)(<video\s(?:[^>]*\s)?poster\s*=\s*["'])(.*?)(["'][^>]*>)''',
                        self.get_inline_video_poster, elt
                    )
                    # Handle video element's source sub-element's src attribute
                    elt = re.sub(r'''(?i)(<source\s(?:[^>]*\s)?src\s*=\s*["'])(.*?)(["'][^>]*>)''',
                        self.get_inline_video, elt
                    )
                append(elt, fmt)

        if self.typ == 'text':
            self.title = get_try('title')
            append_try('body')

        elif self.typ == 'photo':
            url = get_try('link_url')
            is_photoset = len(post['photos']) > 1
            for offset, p in enumerate(post['photos'], start=1):
                o = p['alt_sizes'][0] if 'alt_sizes' in p else p['original_size']
                src = o['url']
                if options.save_images:
                    src = self.get_image_url(src, offset if is_photoset else 0)
                append(escape(src), u'<img alt="" src="%s">')
                if url:
                    content[-1] = u'<a href="%s">%s</a>' % (escape(url), content[-1])
                content[-1] = '<p>' + content[-1] + '</p>'
                if p['caption']:
                    append(p['caption'], u'<p>%s</p>')
            append_try('caption')

        elif self.typ == 'link':
            url = post['url']
            self.title = u'<a href="%s">%s</a>' % (escape(url), post['title'] or url)
            append_try('description')

        elif self.typ == 'quote':
            append(post['text'], u'<blockquote><p>%s</p></blockquote>')
            append_try('source', u'<p>%s</p>')

        elif self.typ == 'video':
            src = ''
            if (options.save_video or options.save_video_tumblr) \
                    and post['video_type'] == 'tumblr':
                src = self.get_media_url(post['video_url'], '.mp4')
            elif options.save_video:
                src = self.get_youtube_url(self.url)
                if not src:
                    log('Unable to download video in post #{}\n'.format(self.ident))
            if src:
                append(u'<p><video controls><source src="%s" type=video/mp4>%s<br>\n<a href="%s">%s</a></video></p>' % (
                    src, "Your browser does not support the video element.", src, "Video file"
                ))
            else:
                player = get_try('player')
                if player:
                    append(player[-1]['embed_code'])
                else:
                    append_try('video_url')
            append_try('caption')

        elif self.typ == 'audio':
            def make_player(src_):
                append(u'<p><audio controls><source src="%s" type=audio/mpeg>%s<br>\n<a href="%s">%s</a></audio></p>'
                       % (src_, "Your browser does not support the audio element.", src_, "Audio file"))

            src = None
            audio_url = get_try('audio_url') or get_try('audio_source_url')
            if options.save_audio:
                if post['audio_type'] == 'tumblr':
                    if audio_url.startswith('https://a.tumblr.com/'):
                        src = self.get_media_url(audio_url, '.mp3')
                    elif audio_url.startswith('https://www.tumblr.com/audio_file/'):
                        audio_url = u'https://a.tumblr.com/%so1.mp3' % audio_url.split('/')[-1]
                        src = self.get_media_url(audio_url, '.mp3')
                elif post['audio_type'] == 'soundcloud':
                    src = self.get_media_url(audio_url, '.mp3')
            player = get_try('player')
            if src:
                make_player(src)
            elif player:
                append(player)
            elif audio_url:
                make_player(audio_url)
            append_try('caption')

        elif self.typ == 'answer':
            self.title = post['question']
            append_try('answer')

        elif self.typ == 'chat':
            self.title = get_try('title')
            append(
                u'<br>\n'.join('%(label)s %(phrase)s' % d for d in post['dialogue']),
                u'<p>%s</p>'
            )

        else:
            log(u"Unknown post type '{}' in post #{}\n".format(self.typ, self.ident))
            append(escape(self.json_content), u'<pre>%s</pre>')

        self.content = '\n'.join(content)

        # fix wrongly nested HTML elements
        for p in ('<p>(<(%s)>)', '(</(%s)>)</p>'):
            self.content = re.sub(p % 'p|ol|iframe[^>]*', r'\1', self.content)

        self.save_post()

    def get_youtube_url(self, youtube_url):
        # determine the media file name
        filetmpl = u'%(id)s_%(uploader_id)s_%(title)s.%(ext)s'
        ydl_options = {
            'outtmpl': join(self.media_folder, filetmpl),
            'quiet': True,
            'restrictfilenames': True,
            'noplaylist': True,
            'continuedl': True,
            'nooverwrites': True,
            'retries': 3000,
            'fragment_retries': 3000,
            'ignoreerrors': True,
        }
        if options.cookiefile is not None:
            ydl_options['cookiefile'] = options.cookiefile
        ydl = youtube_dl.YoutubeDL(ydl_options)
        ydl.add_default_info_extractors()
        try:
            result = ydl.extract_info(youtube_url, download=False)
            media_filename = sanitize_filename(filetmpl % result['entries'][0], restricted=True)
        except Exception:
            return ''

        # check if a file with this name already exists
        if not os.path.isfile(media_filename):
            try:
                ydl.extract_info(youtube_url, download=True)
            except Exception:
                return ''
        return u'%s/%s' % (self.media_url, split(media_filename)[1])

    def get_media_url(self, media_url, extension):
        if not media_url:
            return ''
        media_filename = self.get_filename(media_url)
        media_filename = os.path.splitext(media_filename)[0] + extension
        saved_name = self.download_media(media_url, media_filename)
        if saved_name is not None:
            media_filename = u'%s/%s' % (self.media_url, saved_name)
        return media_filename

    def get_image_url(self, image_url, offset):
        """Saves an image if not saved yet. Returns the new URL or
        the original URL in case of download errors."""
        image_filename = self.get_filename(image_url, '_o%s' % offset if offset else '')
        saved_name = self.download_media(image_url, image_filename)
        if saved_name is not None:
            if options.exif and saved_name.endswith('.jpg'):
                add_exif(join(self.media_folder, saved_name), set(self.tags))
            image_url = u'%s/%s' % (self.media_url, saved_name)
        return image_url

    @staticmethod
    def maxsize_image_url(image_url):
        if ".tumblr.com/" not in image_url or image_url.endswith('.gif'):
            return image_url
        # change the image resolution to 1280
        return re.sub(r'_\d{2,4}(\.\w+)$', r'_1280\1', image_url)

    def get_inline_image(self, match):
        """Saves an inline image if not saved yet. Returns the new <img> tag or
        the original one in case of download errors."""
        image_url, image_filename = self._parse_url_match(match, transform=self.maxsize_image_url)
        if not image_filename or not image_url.startswith('http'):
            return match.group(0)
        saved_name = self.download_media(image_url, image_filename)
        if saved_name is None:
            return match.group(0)
        return u'%s%s/%s%s' % (match.group(1), self.media_url,
            saved_name, match.group(3)
        )

    def get_inline_video_poster(self, match):
        """Saves an inline video poster if not saved yet. Returns the new
        <video> tag or the original one in case of download errors."""
        poster_url, poster_filename = self._parse_url_match(match)
        if not poster_filename or not poster_url.startswith('http'):
            return match.group(0)
        saved_name = self.download_media(poster_url, poster_filename)
        if saved_name is None:
            return match.group(0)
        # get rid of autoplay and muted attributes to align with normal video
        # download behaviour
        return (u'%s%s/%s%s' % (match.group(1), self.media_url,
            saved_name, match.group(3)
        )).replace('autoplay="autoplay"', '').replace('muted="muted"', '')

    def get_inline_video(self, match):
        """Saves an inline video if not saved yet. Returns the new <video> tag
        or the original one in case of download errors."""
        video_url, video_filename = self._parse_url_match(match)
        if not video_filename or not video_url.startswith('http'):
            return match.group(0)
        saved_name = None
        if '.tumblr.com' in video_url:
            saved_name = self.get_media_url(video_url, '.mp4')
        elif options.save_video:
            saved_name = self.get_youtube_url(video_url)
        if saved_name is None:
            return match.group(0)
        return u'%s%s%s' % (match.group(1), saved_name, match.group(3))

    def get_filename(self, url, offset=''):
        """Determine the image file name depending on options.image_names"""
        if options.image_names == 'i':
            return self.ident + offset
        if options.image_names == 'bi':
            return self.backup_account + '_' + self.ident + offset
        fname = url.split('/')[-1]
        # delete characters not allowed under Windows
        return re.sub(r'[:<>"/\\|*?]', '', fname) if os.name == 'nt' else fname

    def download_media(self, url, filename):
        # check if a file with this name already exists
        known_extension = '.' in filename[-5:]
        image_glob = glob(path_to(self.media_dir,
            filename + ('' if known_extension else '.*')
        ))
        if image_glob:
            return split(image_glob[0])[1]
        # download the media data
        try:
            resp = tb_urlopen(url)
            with open_media(self.media_dir, filename) as dest:
                data = resp.read(HTTP_CHUNK_SIZE)
                hdr = data[:32]  # save the first few bytes
                while data:
                    dest.write(data)
                    data = resp.read(HTTP_CHUNK_SIZE)
        except (EnvironmentError, ValueError, HTTPException) as e:
            sys.stderr.write('%s downloading %s\n' % (e, url))
            try:
                os.unlink(path_to(self.media_dir, filename))
            except EnvironmentError as ee:
                if getattr(ee, 'errno', None) != errno.ENOENT:
                    raise

            return None
        # determine the file type if it's unknown
        if not known_extension:
            image_type = imghdr.what(None, hdr)
            if image_type:
                oldname = path_to(self.media_dir, filename)
                filename += '.' + image_type.replace('jpeg', 'jpg')
                os.rename(oldname, path_to(self.media_dir, filename))
        return filename

    def get_post(self):
        """returns this post in HTML"""
        typ = (u'liked-' if options.likes else u'') + self.typ
        post = self.post_header + u'<article class=%s id=p-%s>\n' % (typ, self.ident)
        post += u'<header>\n'
        if options.likes:
            post += u'<p><a href=\"https://{0}.tumblr.com/\" class=\"tumblr_blog\">{0}</a>:</p>\n'.format(self.creator)
        post += u'<p><time datetime=%s>%s</time>\n' % (self.isodate, strftime('%x %X', self.tm))
        post += u'<a class=llink href=%s%s/%s>¶</a>\n' % (save_dir, post_dir, self.llink)
        post += u'<a href=%s>●</a>\n' % self.shorturl
        if self.reblogged_from and self.reblogged_from != self.reblogged_root:
            post += u'<a href=%s>⬀</a>\n' % self.reblogged_from
        if self.reblogged_root:
            post += u'<a href=%s>⬈</a>\n' % self.reblogged_root
        post += '</header>\n'
        if self.title:
            post += u'<h2>%s</h2>\n' % self.title
        post += self.content
        foot = []
        if self.tags:
            foot.append(u''.join(self.tag_link(t) for t in self.tags))
        if self.note_count:
            foot.append(u'%d note%s' % (self.note_count, 's'[self.note_count == 1:]))
        if self.source_title and self.source_url:
            foot.append(u'<a title=Source href=%s>%s</a>' %
                (self.source_url, self.source_title)
            )
        if foot:
            post += u'\n<footer>%s</footer>' % u' — '.join(foot)
        post += u'\n</article>\n'
        return post

    @staticmethod
    def tag_link(tag):
        tag_disp = escape(TAG_FMT.format(tag))
        if not TAGLINK_FMT:
            return tag_disp + u' '
        url = TAGLINK_FMT.format(domain=blog_name, tag=quote(to_bytes(tag)))
        return u'<a href=%s>%s</a>\n' % (url, tag_disp)

    def save_post(self):
        """saves this post locally"""
        if options.dirs:
            f = open_text(post_dir, self.ident, dir_index)
        else:
            f = open_text(post_dir, self.file_name)
        with f:
            f.write(self.get_post())
        os.utime(f.name, (self.date, self.date))
        if options.json:
            with open_text(json_dir, self.ident + '.json') as f:
                f.write(self.json_content)

    @staticmethod
    def _parse_url_match(match, transform=None):
        url = match.group(2)
        if url.startswith('//'):
            url = 'https:' + url
        if transform is not None:
            url = transform(url)
        path = urlparse(url).path
        filename = path.split('/')[-1]
        return url, filename


class BlosxomPost(TumblrPost):
    def get_image_url(self, image_url, offset):
        return image_url

    def get_post(self):
        """returns this post as a Blosxom post"""
        post = self.title + '\nmeta-id: p-' + self.ident + '\nmeta-url: ' + self.url
        if self.tags:
            post += '\nmeta-tags: ' + ' '.join(t.replace(' ', '+') for t in self.tags)
        post += '\n\n' + self.content
        return post


class LocalPost(object):
    def __init__(self, post_file):
        self.post_file = post_file
        if options.tag_index:
            with io.open(post_file, encoding=FILE_ENCODING) as f:
                post = f.read()
            # extract all URL-encoded tags
            self.tags = []  # type: List[Tuple[str, str]]
            footer_pos = post.find('<footer>')
            if footer_pos > 0:
                self.tags = re.findall(r'<a.+?/tagged/(.+?)>#(.+?)</a>', post[footer_pos:])
        parts = post_file.split(os.sep)
        if parts[-1] == dir_index:  # .../<post_id>/index.html
            self.file_name = os.sep.join(parts[-2:])
            self.ident = parts[-2]
        else:
            self.file_name = parts[-1]
            self.ident = splitext(self.file_name)[0]
        self.date = os.stat(post_file).st_mtime  # type: float
        self.tm = time.localtime(self.date)

    def get_post(self, in_tag_index):
        with io.open(self.post_file, encoding=FILE_ENCODING) as f:
            post = f.read()
        # remove header and footer
        lines = post.split('\n')
        while lines and '<article ' not in lines[0]:
            del lines[0]
        while lines and '</article>' not in lines[-1]:
            del lines[-1]
        post = '\n'.join(lines)
        if in_tag_index:
            # fixup all media links which now have to be two folders lower
            shallow_media = '../' + media_dir
            deep_media = save_dir + media_dir
            post = post.replace(shallow_media, deep_media)
        return post


class ThreadPool(object):
    def __init__(self, account, thread_count=20, max_queue=1000):
        self.account = account
        self.queue = queue.Queue(max_queue)  # type: Queue[Callable[[], None]]
        self.quit = threading.Event()
        self.abort = threading.Event()
        self.threads = [threading.Thread(target=self.handler) for _ in range(thread_count)]
        for t in self.threads:
            t.start()

    def add_work(self, work):
        self.queue.put(work)

    def wait(self):
        self.quit.set()
        self.queue.join()

    def cancel(self):
        self.abort.set()
        for i, t in enumerate(self.threads, start=1):
            log('Stopping threads {}{}\r'.format(' ' * i, '.' * (len(self.threads) - i)))
            t.join()

    def handler(self):
        while not self.abort.is_set():
            try:
                work = self.queue.get(timeout=0.1)
            except queue.Empty:
                if self.quit.is_set():
                    break
                continue

            qsize = self.queue.qsize()
            if self.quit.is_set() and qsize % MAX_POSTS == 0:
                log('{} remaining posts to save\r'.format(qsize), self.account)

            try:
                work()
            finally:
                self.queue.task_done()


if __name__ == '__main__':
    import argparse

    class CSVCallback(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, set(values.split(',')))

    class RequestCallback(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            request = getattr(namespace, self.dest) or {}
            for req in values.lower().split(','):
                parts = req.strip().split(':')
                typ = parts.pop(0)
                if typ != TYPE_ANY and typ not in POST_TYPES:
                    parser.error("{}: invalid post type '{}'".format(option_string, typ))
                for typ in POST_TYPES if typ == TYPE_ANY else (typ,):
                    if parts:
                        request[typ] = request.get(typ, set()).union(parts)
                    else:
                        request[typ] = {TAG_ANY}
            setattr(namespace, self.dest, request)

    class TagsCallback(RequestCallback):
        def __call__(self, parser, namespace, values, option_string=None):
            super(TagsCallback, self).__call__(
                parser, namespace, TYPE_ANY + ':' + values.replace(',', ':'), option_string,
            )

    parser = argparse.ArgumentParser(usage='%(prog)s [options] blog-name ...',
                                     description='Makes a local backup of Tumblr blogs.')
    parser.add_argument('-O', '--outdir', help='set the output directory (default: blog-name)')
    parser.add_argument('-D', '--dirs', action='store_true', help='save each post in its own folder')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress progress messages')
    parser.add_argument('-i', '--incremental', action='store_true', help='incremental backup mode')
    parser.add_argument('-l', '--likes', action='store_true', help="save a blog's likes, not its posts")
    parser.add_argument('-k', '--skip-images', action='store_false', dest='save_images',
                        help='do not save images; link to Tumblr instead')
    parser.add_argument('--save-video', action='store_true', help='save all video files')
    parser.add_argument('--save-video-tumblr', action='store_true', help='save only Tumblr video files')
    parser.add_argument('--save-audio', action='store_true', help='save audio files')
    parser.add_argument('--cookiefile', help='cookie file for youtube-dl')
    parser.add_argument('-j', '--json', action='store_true', help='save the original JSON source')
    parser.add_argument('-b', '--blosxom', action='store_true', help='save the posts in blosxom format')
    parser.add_argument('-r', '--reverse-month', action='store_false',
                        help='reverse the post order in the monthly archives')
    parser.add_argument('-R', '--reverse-index', action='store_false', help='reverse the index file order')
    parser.add_argument('--tag-index', action='store_true', help='also create an archive per tag')
    parser.add_argument('-a', '--auto', type=int, metavar='HOUR',
                        help='do a full backup at HOUR hours, otherwise do an incremental backup'
                             ' (useful for cron jobs)')
    parser.add_argument('-n', '--count', type=int, help='save only COUNT posts')
    parser.add_argument('-s', '--skip', type=int, default=0, help='skip the first SKIP posts')
    parser.add_argument('-p', '--period', help="limit the backup to PERIOD ('y', 'm', 'd' or YYYY[MM[DD]])")
    parser.add_argument('-N', '--posts-per-page', type=int, default=50, metavar='COUNT',
                        help='set the number of posts per monthly page, 0 for unlimited')
    parser.add_argument('-Q', '--request', action=RequestCallback,
                        help=u'save posts matching the request TYPE:TAG:TAG:…,TYPE:TAG:…,…. '
                             u'TYPE can be {} or {any}; TAGs can be omitted or a colon-separated list. '
                             u'Example: -Q {any}:personal,quote,photo:me:self'
                             .format(u', '.join(POST_TYPES), any=TYPE_ANY))
    parser.add_argument('-t', '--tags', action=TagsCallback, dest='request',
                        help='save only posts tagged TAGS (comma-separated values; case-insensitive)')
    parser.add_argument('-T', '--type', action=RequestCallback, dest='request',
                        help='save only posts of type TYPE (comma-separated values from {})'
                             .format(', '.join(POST_TYPES)))
    parser.add_argument('--no-reblog', action='store_true', help="don't save reblogged posts")
    parser.add_argument('-I', '--image-names', choices=('o', 'i', 'bi'), default='o', metavar='FMT',
                        help="image filename format ('o'=original, 'i'=<post-id>, 'bi'=<blog-name>_<post-id>)")
    parser.add_argument('-e', '--exif', action=CSVCallback, default=set(), metavar='KW',
                        help='add EXIF keyword tags to each picture'
                             " (comma-separated values; '-' to remove all tags, '' to add no extra tags)")
    parser.add_argument('-S', '--no-ssl-verify', action='store_true', help='ignore SSL verification errors')
    parser.add_argument('blogs', nargs='*')
    options = parser.parse_args()

    if options.auto is not None and options.auto != time.localtime().tm_hour:
        options.incremental = True
    if options.period:
        try:
            pformat = {'y': '%Y', 'm': '%Y%m', 'd': '%Y%m%d'}[options.period]
            options.period = time.strftime(pformat)
        except KeyError:
            options.period = options.period.replace('-', '')
            if not re.match(r'^\d{4}(\d\d)?(\d\d)?$', options.period):
                parser.error("Period must be 'y', 'm', 'd' or YYYY[MM[DD]]")
        set_period()
    if have_ssl_ctx and options.no_ssl_verify:
        ssl_ctx = ssl._create_unverified_context()
        # Otherwise, it's an old Python version without SSL verification,
        # so this is the default.

    blogs = options.blogs or DEFAULT_BLOGS
    if not blogs:
        parser.error("Missing blog-name")
    if options.count is not None and options.count < 0:
        parser.error('--count: count must not be negative')
    if options.count == 0 and (options.incremental or options.auto is not None):
        parser.error('--count 0 conflicts with --incremental and --auto')
    if options.skip < 0:
        parser.error('--skip: skip must not be negative')
    if options.posts_per_page < 0:
        parser.error('--posts-per-page: posts per page must not be negative')
    if options.outdir and len(blogs) > 1:
        parser.error("-O can only be used for a single blog-name")
    if options.dirs and options.tag_index:
        parser.error("-D cannot be used with --tag-index")
    if options.exif:
        if pyexiv2 is None:
            parser.error("--exif: module 'pyexiv2' is not installed")
        if not hasattr(pyexiv2, 'ImageMetadata'):
            parser.error("--exif: module 'pyexiv2' is missing features, perhaps you need 'py3exiv2'?")
    if options.save_video and not youtube_dl:
        parser.error("--save-video: module 'youtube_dl' is not installed")
    if options.cookiefile is not None and not os.access(options.cookiefile, os.R_OK):
        parser.error('--cookiefile: file cannot be read')

    if not API_KEY:
        sys.stderr.write('''\
Missing API_KEY; please get your own API key at
https://www.tumblr.com/oauth/apps\n''')
        sys.exit(1)

    tb = TumblrBackup()
    try:
        for account in blogs:
            tb.backup(account)
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPT)

    sys.exit(tb.exit_code())
