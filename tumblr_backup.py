#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

# standard Python library imports
import errno
import hashlib
import imghdr
import io
import itertools
import locale
import multiprocessing
import os
import re
import shutil
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from glob import glob
from os.path import join, split, splitext
from posixpath import basename as urlbasename, join as urlpathjoin, splitext as urlsplitext
from xml.sax.saxutils import escape

from util import (ConnectionFile, LockedQueue, PY3, is_dns_working, make_requests_session, no_internet, nullcontext,
                  path_is_on_vfat, to_bytes, to_unicode)
from wget import HTTPError, HTTP_RETRY, HTTP_TIMEOUT, WGError, WgetRetrieveWrapper, setup_wget, urlopen

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from queue import Queue
    from typing import Any, Callable, DefaultDict, Dict, Iterable, List, Optional, Set, Text, Tuple, Type

    JSONDict = Dict[str, Any]

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
except ImportError:
    from urllib import quote, urlencode  # type: ignore[attr-defined,no-redef]
    from urlparse import urlparse  # type: ignore[no-redef]

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

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import pyjq
except ImportError:
    pyjq = None

try:
    from os import DirEntry, scandir  # type: ignore[attr-defined]
except ImportError:
    try:
        from scandir import DirEntry, scandir  # type: ignore[no-redef]
    except ImportError:
        scandir = None  # type: ignore[assignment,no-redef]

# NB: setup_urllib3_ssl has already been called by wget

try:
    import requests
except ImportError:
    if not TYPE_CHECKING:
        try:
            from pip._vendor import requests  # type: ignore[no-redef]
        except ImportError:
            raise RuntimeError('The requests module is required. Please install it with pip or your package manager.')

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
save_dir = '..'
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
REM_POST_INC = 10

# get your own API key at https://www.tumblr.com/oauth/apps
API_KEY = ''

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass
FILE_ENCODING = 'utf-8'
TIME_ENCODING = locale.getlocale(locale.LC_TIME)[1] or FILE_ENCODING

disable_note_scraper = set()  # type: Set[str]
disablens_lock = threading.Lock()
prev_resps = None  # type: Optional[Tuple[str, ...]]


class Logger(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.backup_account = None  # type: Optional[str]
        self.status_msg = None  # type: Optional[str]

    def __call__(self, msg, account=False):
        with self.lock:
            for line in msg.splitlines(True):
                self._print(line, account)
            if self.status_msg:
                self._print(self.status_msg, account=True)
            sys.stdout.flush()

    def status(self, msg):
        self.status_msg = msg
        self('')

    def _print(self, msg, account=False):
        if options.quiet:
            return
        if account:  # Optional account prefix
            msg = '{}: {}'.format(self.backup_account, msg)

        # Separate terminator
        it = (i for i, c in enumerate(reversed(msg)) if c not in '\r\n')
        try:
            idx = len(msg) - next(it)
        except StopIteration:
            idx = 0
        msg, term = msg[:idx], msg[idx:]

        pad = ' ' * (80 - len(msg))  # Pad to 80 chars
        print(msg + pad + term, end='')


log = Logger()


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

    options.p_start = int(mktime(tm))
    tm[i] += 1
    options.p_stop = int(mktime(tm))


class ApiParser(object):
    session = None  # type: Optional[requests.Session]

    def __init__(self, base, account):
        self.base = base
        self.account = account
        self.prev_resps = None  # type: Optional[Tuple[str, ...]]
        self.dashboard_only_blog = None  # type: Optional[bool]

    @classmethod
    def setup(cls):
        cls.session = make_requests_session(
            requests.Session, HTTP_RETRY, HTTP_TIMEOUT,
            not options.no_ssl_verify, options.user_agent, options.cookiefile,
        )

    def read_archive(self, prev_archive):
        def read_resp(path):
            with io.open(path, encoding=FILE_ENCODING) as jf:
                return json.load(jf)

        if options.likes:
            log('Reading liked timestamps from saved responses (may take a while)\n', account=True)

        self.prev_resps = tuple(
            e.path for e in sorted(
                (e for e in scandir(join(prev_archive, 'json')) if (e.name.endswith('.json') and e.is_file())),
                key=lambda e: read_resp(e)['liked_timestamp'] if options.likes else long(e.name[:-5]),
                reverse=True,
            )
        )

    def apiparse(self, count, start=0, before=None):
        # type: (...) -> Optional[JSONDict]
        assert self.session is not None
        if self.prev_resps is not None:
            # Reconstruct the API response
            def read_post(prf):
                with io.open(prf, encoding=FILE_ENCODING) as f:
                    try:
                        post = json.load(f)
                    except ValueError as e:
                        f.seek(0)
                        log('{}: {}\n{!r}\n'.format(e.__class__.__name__, e, f.read()))
                        return None
                return prf, post
            posts = map(read_post, self.prev_resps)  # type: Iterable[Tuple[DirEntry[str], JSONDict]]
            if before is not None:
                posts = itertools.dropwhile(
                    lambda pp: pp[1]['liked_timestamp' if options.likes else 'timestamp'] >= before,
                    posts,
                )
            posts = list(itertools.islice(posts, start, start + count))
            return {'posts': [post for prf, post in posts],
                    'post_respfiles': [prf for prf, post in posts],
                    'blog': dict(posts[0][1]['blog'] if posts else {}, posts=len(self.prev_resps))}

        if self.dashboard_only_blog:
            base = 'https://www.tumblr.com/svc/indash_blog'
            params = {'tumblelog_name_or_id': self.account, 'post_id': '', 'limit': count,
                      'should_bypass_safemode': 'true', 'should_bypass_tagfiltering': 'true'}
            headers = {
                'Referer': 'https://www.tumblr.com/dashboard/blog/' + self.account,
                'X-Requested-With': 'XMLHttpRequest',
            }  # type: Optional[Dict[str, str]]
        else:
            base = self.base
            params = {'api_key': API_KEY, 'limit': count, 'reblog_info': 'true'}
            headers = None
        if before:
            params['before'] = before
        if start > 0 and not options.likes:
            params['offset'] = start

        sleep_dur = 30  # in seconds
        while True:
            doc = self._get_resp(base, params, headers)
            if doc is None:
                return None
            status = doc['meta']['status']
            if status != 429:
                break
            time.sleep(sleep_dur)
            sleep_dur *= 2
        if status != 200:
            # Detect dashboard-only blogs by the error codes
            if self.dashboard_only_blog is None and status == 404:
                errors = doc.get('errors', ())
                if len(errors) == 1 and errors[0].get('code') == 4012:
                    self.dashboard_only_blog = True
                    log('Found dashboard-only blog, trying svc API\n', account=True)
                    return self.apiparse(count, start)  # Recurse once
            log('API response has non-200 status:\n{}\n'.format(doc))
            if status == 401 and self.dashboard_only_blog:
                log("This is a dashboard-only blog, so you probably don't have the right cookies.{}\n".format(
                    '' if options.cookiefile else ' Try --cookiefile.',
                ))
            return None
        # If the first API request succeeds, it's a public blog
        if self.dashboard_only_blog is None:
            self.dashboard_only_blog = False
        resp = doc.get('response')
        if resp is not None and self.dashboard_only_blog:
            # svc API doesn't return blog info, steal it from the first post
            resp['blog'] = resp['posts'][0]['blog'] if resp['posts'] else {}
        return resp

    def _get_resp(self, base, params, headers):
        assert self.session is not None
        while True:
            try:
                with self.session.get(base, params=params, headers=headers) as resp:
                    if not (200 <= resp.status_code < 300 or 400 <= resp.status_code < 500):
                        log('URL is {}?{}\nError retrieving API repsonse: HTTP {} {}\n'.format(
                            base, urlencode(params), resp.status_code, resp.reason,
                        ))
                        return None
                    ctype = resp.headers.get('Content-Type')
                    if ctype and ctype.split(';', 1)[0].strip() != 'application/json':
                        log("Unexpected Content-Type: '{}'\n".format(ctype))
                        return None
                    try:
                        return resp.json()
                    except ValueError as e:
                        log('{}: {}\n{} {} {}\n{!r}\n'.format(
                            e.__class__.__name__, e, resp.status_code, resp.reason, ctype, resp.content.decode('utf-8'),
                        ))
                        return None
            except (EnvironmentError, HTTPError) as e:
                if isinstance(e, HTTPError) and not is_dns_working(timeout=5):
                    no_internet.signal()
                    continue
                log('URL is {}?{}\nError retrieving API repsonse: {}\n'.format(base, urlencode(params), e))
                return None


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


def get_avatar(prev_archive):
    if prev_archive is not None:
        # Copy old avatar, if present
        avatar_glob = glob(join(prev_archive, theme_dir, avatar_base + '.*'))
        if avatar_glob:
            src = avatar_glob[0]
            path_parts = (theme_dir, split(src)[-1])
            cpy_res = maybe_copy_media(prev_archive, path_parts)
            if cpy_res:
                return  # We got the avatar

    url = 'https://api.tumblr.com/v2/blog/%s/avatar' % blog_name
    avatar_dest = avatar_fpath = open_file(lambda f: f, (theme_dir, avatar_base))

    # Remove old avatars
    old_avatars = glob(join(theme_dir, avatar_base + '.*'))
    if len(old_avatars) > 1:
        for old_avatar in old_avatars:
            os.unlink(old_avatar)
    elif len(old_avatars) == 1:
        # Use the old avatar for timestamping
        avatar_dest, = old_avatars

    def adj_bn(old_bn, f):
        # Give it an extension
        image_type = imghdr.what(f)
        if image_type:
            return avatar_fpath + '.' + image_type
        return avatar_fpath

    # Download the image
    try:
        wget_retrieve(url, avatar_dest, adjust_basename=adj_bn)
    except WGError as e:
        e.log()


def get_style(prev_archive):
    """Get the blog's CSS by brute-forcing it from the home page.
    The v2 API has no method for getting the style directly.
    See https://groups.google.com/d/msg/tumblr-api/f-rRH6gOb6w/sAXZIeYx5AUJ"""
    if prev_archive is not None:
        # Copy old style, if present
        path_parts = (theme_dir, 'style.css')
        cpy_res = maybe_copy_media(prev_archive, path_parts)
        if cpy_res:
            return  # We got the style

    url = 'https://%s/' % blog_name
    try:
        resp = urlopen(url)
        page_data = resp.data
    except HTTPError as e:
        log('URL is {}\nError retrieving style: {}\n'.format(url, e))
        return
    for match in re.findall(br'(?s)<style type=.text/css.>(.*?)</style>', page_data):
        css = match.strip().decode('utf-8', errors='replace')
        if '\n' not in css:
            continue
        css = css.replace('\r', '').replace('\n    ', '\n')
        with open_text(theme_dir, 'style.css') as f:
            f.write(css + '\n')
        return


# Copy media file, if present in prev_archive
def maybe_copy_media(prev_archive, path_parts):
    if prev_archive is None:
        return False  # Source does not exist

    srcpath = join(prev_archive, *path_parts)
    dstpath = open_file(lambda f: f, path_parts)

    if PY3:
        try:
            srcf = io.open(srcpath, 'rb')
        except EnvironmentError as e:
            if getattr(e, 'errno', None) not in (errno.ENOENT, errno.EISDIR):
                raise
            return False  # Source does not exist (Python 3)
    else:
        srcf = nullcontext()

    with srcf:
        if PY3:
            src = srcf.fileno()  # pytype: disable=attribute-error
            def dup(fd): return os.dup(fd)
        else:
            src = srcpath
            def dup(fd): return fd

        try:
            src_st = os.stat(src)
        except EnvironmentError as e:
            if getattr(e, 'errno', None) not in (errno.ENOENT, errno.EISDIR):
                raise
            return False  # Source does not exist (Python 2)

        try:
            dst_st = os.stat(dstpath)  # type: Optional[os.stat_result]
        except EnvironmentError as e:
            if getattr(e, 'errno', None) != errno.ENOENT:
                raise
            dst_st = None  # Destination does not exist yet

        # Do not overwrite if destination is no newer and has the same size
        if (dst_st is None
            or dst_st.st_mtime > src_st.st_mtime
            or dst_st.st_size != src_st.st_size
        ):
            # dup src because open() takes ownership and closes it
            shutil.copyfile(dup(src), dstpath)
            shutil.copystat(src, dstpath)  # type: ignore[arg-type]

        return True  # Either we copied it or we didn't need to


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
                idx.write('<p><a href={}>Tag index</a></p>\n'.format(
                    urlpathjoin(tag_index_dir, dir_index)
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
            idx.write(u'    <li><a href={} title="{} post(s)">{}</a></li>\n'.format(
                urlpathjoin(archive_dir, month_name), len(self.index[year][month]), strftime('%B', tm)
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

        FILE_FMT = '%d-%02d-p%s%s'
        pages_month = pages_per_month(year, month)
        first_file = None  # type: Optional[str]
        for page, start in enumerate(xrange(0, posts_month, posts_page), start=1):

            archive = [self.blog.header(strftime('%B %Y', tm), body_class='archive')]
            archive.extend(p.get_post(self.body_class == 'tag-archive') for p in posts[start:start + posts_page])

            suffix = '/' if options.dirs else post_ext
            file_name = FILE_FMT % (year, month, page, suffix)
            if options.dirs:
                base = urlpathjoin(save_dir, archive_dir)
                arch = open_text(index_dir, archive_dir, file_name, dir_index)
            else:
                base = ''
                arch = open_text(index_dir, archive_dir, file_name)

            if page > 1:
                pp = FILE_FMT % (year, month, page - 1, suffix)
            else:
                py, pm = next_month(-1)
                pp = FILE_FMT % (py, pm, pages_per_month(py, pm), suffix) if py else ''
                first_file = file_name

            if page < pages_month:
                np = FILE_FMT % (year, month, page + 1, suffix)
            else:
                ny, nm = next_month(+1)
                np = FILE_FMT % (ny, nm, 1, suffix) if ny else ''

            archive.append(self.blog.footer(base, pp, np))

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
        save_dir = '../../..'
        mkdir(path_to(tag_index_dir))
        tag_index = [self.blog.header('Tag index', 'tag-index', self.blog.title, avatar=True), '<ul>']
        for tag, index in sorted(self.tags.items(), key=lambda kv: kv[1].name):
            digest = hashlib.md5(to_bytes(tag)).hexdigest()
            index.save_index(tag_index_dir + os.sep + digest,
                u"Tag ‛%s’" % index.name
            )
            tag_index.append(u'    <li><a href={}>{}</a></li>'.format(
                urlpathjoin(digest, dir_index), escape(index.name)
            ))
        tag_index.extend(['</ul>', ''])
        with open_text(tag_index_dir, dir_index) as f:
            f.write(u'\n'.join(tag_index))


class TumblrBackup(object):
    def __init__(self):
        self.errors = False
        self.total_count = 0
        self.post_count = 0
        self.filter_skipped = 0
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
            'index': '', 'tag-index': '..', 'tag-archive': '../..'
        }.get(body_class, save_dir)
        css_rel = urlpathjoin(root_rel, custom_css if have_custom_css else backup_css)
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
                h += '<img src={} alt=Avatar>\n'.format(urlpathjoin(root_rel, theme_dir, split(f[0])[1]))
        if title:
            h += u'<h1>%s</h1>\n' % title
        if subtitle:
            h += u'<p class=subtitle>%s</p>\n' % subtitle
        h += '</header>\n'
        return h

    @staticmethod
    def footer(base, previous_page, next_page):
        f = '<footer><nav>'
        f += '<a href={} rel=index>Index</a>\n'.format(urlpathjoin(save_dir, dir_index))
        if previous_page:
            f += '| <a href={} rel=prev>Previous</a>\n'.format(urlpathjoin(base, previous_page))
        if next_page:
            f += '| <a href={} rel=next>Next</a>\n'.format(urlpathjoin(base, next_page))
        f += '</nav></footer>\n'
        return f

    @staticmethod
    def get_post_timestamps(posts):
        for post in posts:
            with io.open(post, encoding=FILE_ENCODING) as pf:
                soup = BeautifulSoup(pf, 'lxml')
            postdate = soup.find('time')['datetime']
            del soup
            # No datetime.fromisoformat or datetime.timestamp on Python 2
            yield (datetime.strptime(postdate, '%Y-%m-%dT%H:%M:%SZ') - datetime(1970, 1, 1)) // timedelta(seconds=1)

    def backup(self, account, prev_archive):
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
                save_dir = '../..'
                mkdir(path_to(post_dir), recursive=True)
            else:
                mkdir(save_folder, recursive=True)
            post_class = TumblrPost
            have_custom_css = os.access(path_to(custom_css), os.R_OK)

        self.post_count = 0
        self.filter_skipped = 0

        # get the highest post id already saved
        ident_max = None
        if options.incremental:
            filter_ = join('*', dir_index) if options.dirs else '*' + post_ext
            post_glob = glob(path_to(post_dir, filter_))
            if not post_glob:
                pass  # No posts to read
            elif options.likes:
                # Read every post to find the newest timestamp we've saved.
                if BeautifulSoup is None:
                    raise RuntimeError("Incremental likes backup: module 'bs4' is not installed")
                log('Finding newest liked post (may take a while)\n', account=True)
                ident_max = max(self.get_post_timestamps(post_glob))
            else:
                ident_max = max(long(splitext(split(f)[1])[0]) for f in post_glob)
            if ident_max is not None:
                log('Backing up posts after {}\n'.format(ident_max), account=True)

        log.status('Getting basic information\r')

        api_parser = ApiParser(base, account)
        if prev_archive:
            api_parser.read_archive(prev_archive)
        resp = api_parser.apiparse(1)
        if not resp:
            self.errors = True
            return

        # collect all the meta information
        if options.likes:
            if not resp.get('blog', {}).get('share_likes', True):
                print('{} does not have public likes\n'.format(account))
                self.errors = True
                return
            posts_key = 'liked_posts'
            blog = {}
            count_estimate = resp['liked_count']
        else:
            posts_key = 'posts'
            blog = resp.get('blog', {})
            count_estimate = blog.get('posts')
        self.title = escape(blog.get('title', account))
        self.subtitle = blog.get('description', '')

        # use the meta information to create a HTML header
        TumblrPost.post_header = self.header(body_class='post')

        # start the thread pool
        backup_pool = ThreadPool()

        # returns whether any posts from this batch were saved
        def _backup(posts, post_respfiles):
            def sort_key(x): return x[0]['liked_timestamp'] if options.likes else long(x[0]['id'])
            sorted_posts = sorted(zip(posts, post_respfiles), key=sort_key, reverse=True)
            for p, prf in sorted_posts:
                no_internet.check()
                post = post_class(p, account, prf, prev_archive)
                if ident_max is None:
                    pass  # No limit
                elif (p['liked_timestamp'] if options.likes else long(post.ident)) <= ident_max:
                    log('Stopping backup: Incremental backup complete\n', account=True)
                    return False
                if options.period:
                    if post.date >= options.p_stop:
                        raise RuntimeError('Found post with date ({}) older than before param ({})'.format(
                            post.date, options.p_stop))
                    if post.date < options.p_start:
                        log('Stopping backup: Reached end of period\n', account=True)
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
                if os.path.exists(path_to(*post.get_path())) and options.no_post_clobber:
                    continue  # Post exists and no-clobber enabled
                if options.filter and not options.filter.first(p):
                    self.filter_skipped += 1
                    continue

                while True:
                    try:
                        backup_pool.add_work(post.save_content, timeout=0.1)
                        break
                    except queue.Full:
                        pass
                    no_internet.check()

                self.post_count += 1
                if options.count and self.post_count >= options.count:
                    log('Stopping backup: Reached limit of {} posts\n'.format(options.count), account=True)
                    return False
            return True

        try:
            # Get the JSON entries from the API, which we can only do for MAX_POSTS posts at once.
            # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
            i = options.skip
            before = options.p_stop if options.period else None
            while True:
                # find the upper bound
                log.status('Getting {}posts {} to {}{}\r'.format(
                    'liked ' if options.likes else '', i, i + MAX_POSTS - 1,
                    '' if count_estimate is None else ' (of {} expected)'.format(count_estimate),
                ))

                resp = api_parser.apiparse(MAX_POSTS, i, before)
                if resp is None:
                    self.errors = True
                    break

                posts = resp[posts_key]
                if not posts:
                    log('Backup complete: Found empty set of posts\n', account=True)
                    break

                post_respfiles = resp.get('post_respfiles')
                if post_respfiles is None:
                    post_respfiles = [None for _ in posts]
                if not _backup(posts, post_respfiles):
                    break

                if options.likes:
                    next_ = resp['_links'].get('next')
                    if next_ is None:
                        log('Backup complete: Found end of likes\n', account=True)
                        break
                    before = next_['query_params']['before']
                i += MAX_POSTS
        except:
            # ensure proper thread pool termination
            backup_pool.cancel()
            raise

        # wait until all posts have been saved
        backup_pool.wait()

        # postprocessing
        if not options.blosxom and (self.post_count or options.count == 0):
            log.status('Getting avatar and style\r')
            get_avatar(prev_archive)
            get_style(prev_archive)
            if not have_custom_css:
                save_style()
            log.status('Building index\r')
            ix = Indices(self)
            ix.build_index()
            ix.save_index()

        log.status(None)
        skipped_msg = (', {} did not match filter'.format(self.filter_skipped)) if self.filter_skipped else ''
        log(
            '{} {}posts backed up{}\n'.format(self.post_count, 'liked ' if options.likes else '', skipped_msg),
            account=True,
        )
        self.total_count += self.post_count


class TumblrPost(object):
    post_header = ''  # set by TumblrBackup.backup()

    def __init__(self, post, backup_account, respfile, prev_archive):
        # type: (JSONDict, str, Text, Text) -> None
        self.content = ''
        self.post = post
        self.backup_account = backup_account
        self.respfile = respfile
        self.prev_archive = prev_archive
        self.creator = post.get('blog_name') or post['tumblelog']
        self.ident = str(post['id'])
        self.url = post['post_url']
        self.shorturl = post['short_url']
        self.typ = str(post['type'])
        self.date = post['liked_timestamp' if options.likes else 'timestamp']  # type: float
        self.isodate = datetime.utcfromtimestamp(self.date).isoformat() + 'Z'
        self.tm = time.localtime(self.date)
        self.title = u''
        self.tags = post['tags']
        self.note_count = post.get('note_count')
        if self.note_count is None:
            self.note_count = post.get('notes', {}).get('count')
        if self.note_count is None:
            self.note_count = 0
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
        self.media_url = urlpathjoin(save_dir, self.media_dir)
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
                append(u'<p><audio controls><source src="{src}" type=audio/mpeg>{}<br>\n<a href="{src}">{}'
                       u'</a></audio></p>'
                       .format('Your browser does not support the audio element.', 'Audio file', src=src_))

            src = None
            audio_url = get_try('audio_url') or get_try('audio_source_url')
            if options.save_audio:
                if post['audio_type'] == 'tumblr':
                    if audio_url.startswith('https://a.tumblr.com/'):
                        src = self.get_media_url(audio_url, '.mp3')
                    elif audio_url.startswith('https://www.tumblr.com/audio_file/'):
                        audio_url = u'https://a.tumblr.com/{}o1.mp3'.format(urlbasename(urlparse(audio_url).path))
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
            append(escape(self.get_json_content()), u'<pre>%s</pre>')

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
        return urlpathjoin(self.media_url, split(media_filename)[1])

    def get_media_url(self, media_url, extension):
        if not media_url:
            return ''
        media_filename = self.get_filename(media_url)
        media_filename = urlsplitext(media_filename)[0] + extension
        saved_name = self.download_media(media_url, media_filename)
        if saved_name is not None:
            return urlpathjoin(self.media_url, saved_name)
        return media_url

    def get_image_url(self, image_url, offset):
        """Saves an image if not saved yet. Returns the new URL or
        the original URL in case of download errors."""
        image_filename = self.get_filename(image_url, '_o%s' % offset if offset else '')
        saved_name = self.download_media(image_url, image_filename)
        if saved_name is not None:
            if options.exif and saved_name.endswith('.jpg'):
                add_exif(join(self.media_folder, saved_name), set(self.tags))
            return urlpathjoin(self.media_url, saved_name)
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
        fname = urlbasename(urlparse(url).path)
        ext = urlsplitext(fname)[1]
        if options.image_names == 'i':
            return self.ident + offset + ext
        if options.image_names == 'bi':
            return self.backup_account + '_' + self.ident + offset + ext
        # delete characters not allowed under Windows
        return re.sub(r'[:<>"/\\|*?]', '', fname) if os.name == 'nt' else fname

    def download_media(self, url, filename):
        parsed_url = urlparse(url, 'http')
        if parsed_url.scheme not in ('http', 'https') or not parsed_url.hostname:
            return None  # This URL does not follow our basic assumptions

        # Make a sane directory to represent the host
        try:
            hostdir = parsed_url.hostname.encode('idna').decode('ascii')
        except UnicodeError:
            hostdir = parsed_url.hostname
        if hostdir in ('.', '..'):
            hostdir = hostdir.replace('.', '%2E')
        if parsed_url.port not in (None, (80 if parsed_url.scheme == 'http' else 443)):
            hostdir += '{}{}'.format('+' if os.name == 'nt' else ':', parsed_url.port)

        path_parts = [self.media_dir, filename]
        if options.hostdirs:
            path_parts.insert(1, hostdir)

        cpy_res = maybe_copy_media(self.prev_archive, path_parts)
        if not cpy_res:
            try:
                wget_retrieve(url, open_file(lambda f: f, path_parts))
            except WGError as e:
                e.log()
                return None

        return filename

    def get_post(self):
        """returns this post in HTML"""
        typ = (u'liked-' if options.likes else u'') + self.typ
        post = self.post_header + u'<article class=%s id=p-%s>\n' % (typ, self.ident)
        post += u'<header>\n'
        if options.likes:
            post += u'<p><a href=\"https://{0}.tumblr.com/\" class=\"tumblr_blog\">{0}</a>:</p>\n'.format(self.creator)
        post += u'<p><time datetime=%s>%s</time>\n' % (self.isodate, strftime('%x %X', self.tm))
        post += u'<a class=llink href={}>¶</a>\n'.format(urlpathjoin(save_dir, post_dir, self.llink))
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
        if self.source_title and self.source_url:
            foot.append(u'<a title=Source href=%s>%s</a>' %
                (self.source_url, self.source_title)
            )

        notes_html = u''

        if options.copy_notes:
            # Copy notes from prev_archive
            with io.open(join(self.prev_archive, post_dir, self.ident + post_ext)) as post_file:
                soup = BeautifulSoup(post_file, 'lxml')
            notes = soup.find('ol', class_='notes')
            if notes is not None:
                notes_html = u''.join([n.prettify() for n in notes.find_all('li')])

        if options.save_notes and self.backup_account not in disable_note_scraper and not notes_html.strip():
            # Scrape and save notes
            while True:
                ns_stdout_rd, ns_stdout_wr = multiprocessing.Pipe(duplex=False)
                ns_msg_rd, ns_msg_wr = multiprocessing.Pipe(duplex=False)
                try:
                    args = (ns_stdout_wr, ns_msg_wr, self.url, self.ident,
                            options.no_ssl_verify, options.user_agent, options.cookiefile, options.notes_limit)
                    process = multiprocessing.Process(target=note_scraper.main, args=args)
                    process.start()
                except:
                    ns_stdout_rd.close()
                    ns_msg_rd.close()
                    raise
                finally:
                    ns_stdout_wr.close()
                    ns_msg_wr.close()

                try:
                    with ConnectionFile(ns_msg_rd) as msg_pipe:
                        for line in msg_pipe:
                            log(line)

                    with ConnectionFile(ns_stdout_rd) as stdout:
                        notes_html = stdout.read()

                    process.join()
                except:
                    process.terminate()
                    process.join()
                    raise

                if process.exitcode == 2:  # EXIT_SAFE_MODE
                    # Safe mode is blocking us, disable note scraping for this blog
                    notes_html = u''
                    with disablens_lock:
                        # Check if another thread already set this
                        if self.backup_account not in disable_note_scraper:
                            disable_note_scraper.add(self.backup_account)
                            log('[Note Scraper] Blocked by safe mode - scraping disabled for {}\n'.format(
                                self.backup_account
                            ))
                elif process.exitcode == 3:  # EXIT_NO_INTERNET
                    no_internet.signal()
                    continue
                break

        notes_str = u'{} note{}'.format(self.note_count, 's'[self.note_count == 1:])
        if notes_html.strip():
            foot.append(u'<details><summary>{}</summary>\n'.format(notes_str))
            foot.append(u'<ol class="notes">')
            foot.append(notes_html)
            foot.append(u'</ol></details>')
        else:
            foot.append(notes_str)

        if foot:
            post += u'\n<footer>{}</footer>'.format(u'\n'.join(foot))
        post += u'\n</article>\n'
        return post

    @staticmethod
    def tag_link(tag):
        tag_disp = escape(TAG_FMT.format(tag))
        if not TAGLINK_FMT:
            return tag_disp + u' '
        url = TAGLINK_FMT.format(domain=blog_name, tag=quote(to_bytes(tag)))
        return u'<a href=%s>%s</a>\n' % (url, tag_disp)

    def get_path(self):
        return (post_dir, self.ident, dir_index) if options.dirs else (post_dir, self.file_name)

    def save_post(self):
        """saves this post locally"""
        with open_text(*self.get_path()) as f:
            f.write(self.get_post())
        os.utime(f.name, (self.date, self.date))
        if options.json:
            with open_text(json_dir, self.ident + '.json') as f:
                f.write(self.get_json_content())

    def get_json_content(self):
        if self.respfile is not None:
            with io.open(self.respfile, encoding=FILE_ENCODING) as f:
                return f.read()
        return to_unicode(json.dumps(self.post, sort_keys=True, indent=4, separators=(',', ': ')))

    @staticmethod
    def _parse_url_match(match, transform=None):
        url = match.group(2)
        if url.startswith('//'):
            url = 'https:' + url
        if transform is not None:
            url = transform(url)
        filename = urlbasename(urlparse(url).path)
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
            self.file_name = join(*parts[-2:])
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
            shallow_media = urlpathjoin('..', media_dir)
            deep_media = urlpathjoin(save_dir, media_dir)
            post = post.replace(shallow_media, deep_media)
        return post


class ThreadPool(object):
    def __init__(self, thread_count=20, max_queue=1000):
        self.queue = LockedQueue(threading.RLock(), max_queue)  # type: LockedQueue[Callable[[], None]]
        self.quit = threading.Event()
        self.abort = threading.Event()
        self.threads = [threading.Thread(target=self.handler) for _ in range(thread_count)]
        for t in self.threads:
            t.start()

    def add_work(self, *args, **kwargs):
        self.queue.put(*args, **kwargs)

    def wait(self):
        log.status('{} remaining posts to save\r'.format(self.queue.qsize()))
        self.quit.set()
        while True:
            with self.queue.all_tasks_done:
                if not self.queue.unfinished_tasks:
                    break
                self.queue.all_tasks_done.wait(timeout=0.1)
            no_internet.check()

    def cancel(self):
        self.abort.set()
        no_internet.destroy()
        for i, t in enumerate(self.threads, start=1):
            log.status('Stopping threads {}{}\r'.format(' ' * i, '.' * (len(self.threads) - i)))
            t.join()

        with self.queue.mutex:
            self.queue.queue.clear()
            self.queue.all_tasks_done.notify_all()

    def handler(self):
        while not self.abort.is_set():
            with self.queue.mutex:
                try:
                    work = self.queue.get(block=not self.quit.is_set(), timeout=0.1)
                except queue.Empty:
                    if self.quit.is_set():
                        break
                    continue
                qsize = self.queue.qsize()

            if self.quit.is_set() and qsize % REM_POST_INC == 0:
                log.status('{} remaining posts to save\r'.format(qsize))

            try:
                work()
            finally:
                self.queue.task_done()


if __name__ == '__main__':
    # The default of 'fork' can cause deadlocks, even on Linux
    # See https://bugs.python.org/issue40399
    if not PY3:
        pass  # No set_start_method. Here be dragons
    elif 'forkserver' in multiprocessing.get_all_start_methods():
        multiprocessing.set_start_method('forkserver')  # Fastest safe option, if supported
    else:
        multiprocessing.set_start_method('spawn')  # Slow but safe

    no_internet.setup()

    import argparse

    class CSVCallback(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, set(values.split(',')))

    class CSVListCallback(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, list(values.split(',')))

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
    parser.add_argument('--save-notes', action='store_true', help='save a list of notes for each post')
    parser.add_argument('--copy-notes', action='store_true', help='copy the notes list from a previous archive')
    parser.add_argument('--notes-limit', type=int, metavar='COUNT', help='limit requested notes to COUNT, per-post')
    parser.add_argument('--cookiefile', help='cookie file for youtube-dl, --save-notes, and svc API')
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
    parser.add_argument('-F', '--filter', help='save posts matching a jq filter (needs pyjq)')
    parser.add_argument('--no-reblog', action='store_true', help="don't save reblogged posts")
    parser.add_argument('-I', '--image-names', choices=('o', 'i', 'bi'), default='o', metavar='FMT',
                        help="image filename format ('o'=original, 'i'=<post-id>, 'bi'=<blog-name>_<post-id>)")
    parser.add_argument('-e', '--exif', action=CSVCallback, default=set(), metavar='KW',
                        help='add EXIF keyword tags to each picture'
                             " (comma-separated values; '-' to remove all tags, '' to add no extra tags)")
    parser.add_argument('-S', '--no-ssl-verify', action='store_true', help='ignore SSL verification errors')
    parser.add_argument('--prev-archives', action=CSVListCallback, default=[], metavar='DIRS',
                        help='comma-separated list of directories (one per blog) containing previous blog archives')
    parser.add_argument('--no-post-clobber', action='store_true', help='Do not re-download existing posts')
    parser.add_argument('-M', '--timestamping', action='store_true',
                        help="don't re-download files if the remote timestamp and size match the local file")
    parser.add_argument('--no-if-modified-since', action='store_false', dest='if_modified_since',
                        help="timestamping: don't send If-Modified-Since header")
    parser.add_argument('--no-server-timestamps', action='store_false', dest='use_server_timestamps',
                        help="don't set local timestamps from HTTP headers")
    parser.add_argument('--mtime-postfix', action='store_true',
                        help="timestamping: work around low-precision mtime on FAT filesystems")
    parser.add_argument('--hostdirs', action='store_true', help='Generate host-prefixed directories for media')
    parser.add_argument('--user-agent', help='User agent string to use with HTTP requests')
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

    wget_retrieve = WgetRetrieveWrapper(options, log)
    setup_wget(not options.no_ssl_verify, options.user_agent)

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
    if options.save_notes:
        if BeautifulSoup is None:
            parser.error("--save-notes: module 'bs4' is not installed")
        import note_scraper
    if options.copy_notes:
        if not options.prev_archives:
            parser.error('--copy-notes requires --prev-archives')
        if BeautifulSoup is None:
            parser.error("--copy-notes: module 'bs4' is not installed")
    if options.notes_limit is not None:
        if not options.save_notes:
            parser.error('--notes-limit requires --save-notes')
        if options.notes_limit < 1:
            parser.error('--notes-limit: Value must be at least 1')
    if options.filter is not None:
        if pyjq is None:
            parser.error("--filter: module 'pyjq' is not installed")
        options.filter = pyjq.compile(options.filter)
    if options.prev_archives:
        if scandir is None:
            parser.error("--prev-archives: Python is less than 3.5 and module 'scandir' is not installed")
        if len(options.prev_archives) != len(blogs):
            parser.error('--prev-archives: expected {} directories, got {}'.format(
                len(blogs), len(options.prev_archives),
            ))
        for blog, pa in zip(blogs, options.prev_archives):
            if not os.access(pa, os.R_OK | os.X_OK):
                parser.error("--prev-archives: directory '{}' cannot be read".format(pa))
            blogdir = os.curdir if options.blosxom else (options.outdir or blog)
            if os.path.realpath(pa) == os.path.realpath(blogdir):
                parser.error("--prev-archives: Directory '{}' is also being written to. Use --reuse-json instead if "
                             "you want this, or specify --outdir if you don't.".format(pa))
    if not options.mtime_postfix and path_is_on_vfat.works and path_is_on_vfat('.'):
        print('Warning: FAT filesystem detected, enabling --mtime-postfix', file=sys.stderr)
        options.mtime_postfix = True

    if not API_KEY:
        sys.stderr.write('''\
Missing API_KEY; please get your own API key at
https://www.tumblr.com/oauth/apps\n''')
        sys.exit(1)

    ApiParser.setup()
    tb = TumblrBackup()
    try:
        for i, account in enumerate(blogs):
            log.backup_account = account
            tb.backup(account, options.prev_archives[i] if options.prev_archives else None)
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPT)

    sys.exit(tb.exit_code())
