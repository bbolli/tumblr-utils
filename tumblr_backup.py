#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function, with_statement

# standard Python library imports
import contextlib
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
import signal
import sys
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from glob import glob
from os.path import join, split, splitext
from posixpath import basename as urlbasename, join as urlpathjoin, splitext as urlsplitext
from tempfile import NamedTemporaryFile
from xml.sax.saxutils import escape

from util import (AsyncCallable, ConnectionFile, LockedQueue, MultiCondition, PY3, disable_unraisable_hook,
                  is_dns_working, make_requests_session, no_internet, nullcontext, opendir, path_is_on_vfat, to_bytes,
                  to_unicode, try_unlink)
from wget import HTTPError, HTTP_TIMEOUT, Retry, WGError, WgetRetrieveWrapper, setup_wget, urlopen

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any, Callable, DefaultDict, Dict, Iterable, List, Optional, Set, Text, Tuple, Type

    JSONDict = Dict[str, Any]

try:
    import json
except ImportError:
    import simplejson as json  # type: ignore[no-redef]

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
        # Import pip._internal.download first to avoid a potential recursive import
        try:
            from pip._internal import download as _  # noqa: F401
        except ImportError:
            pass  # Not absolutely necessary
        try:
            from pip._vendor import requests  # type: ignore[no-redef]
        except ImportError:
            raise RuntimeError('The requests module is required. Please install it with pip or your package manager.')

try:
    from http import client as httplib
except ImportError:
    import httplib  # type: ignore

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

# Always retry on 503 or 504, but never on connect or 429, the latter handled specially
HTTP_RETRY = Retry(3, connect=False, status_forcelist=frozenset((503, 504)))
HTTP_RETRY.RETRY_AFTER_STATUS_CODES = frozenset((413,))

# get your own API key at https://www.tumblr.com/oauth/apps
API_KEY = ''

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass
FILE_ENCODING = 'utf-8'
TIME_ENCODING = locale.getlocale(locale.LC_TIME)[1] or FILE_ENCODING

PREV_MUST_MATCH_OPTIONS = ('likes', 'blosxom')
MEDIA_PATH_OPTIONS = ('dirs', 'hostdirs', 'image_names')
MUST_MATCH_OPTIONS = PREV_MUST_MATCH_OPTIONS + MEDIA_PATH_OPTIONS
BACKUP_CHANGING_OPTIONS = (
    'save_images', 'save_video', 'save_video_tumblr', 'save_audio', 'save_notes', 'copy_notes', 'notes_limit', 'json',
    'count', 'skip', 'period', 'request', 'filter', 'no_reblog', 'exif', 'prev_archives')

main_thread_lock = threading.RLock()
multicond = MultiCondition(main_thread_lock)
disable_note_scraper = set()  # type: Set[str]
disablens_lock = threading.Lock()
prev_resps = None  # type: Optional[Tuple[str, ...]]


def load_bs4(reason):
    sys.modules['soupsieve'] = ()  # type: ignore[assignment]
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("Cannot {} without module 'bs4'".format(reason))
    return BeautifulSoup


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
    mkdir(path_to(*parts[:-1]), recursive=True)
    return open_fn(path_to(*parts))


@contextlib.contextmanager
def open_text(*parts):
    dest_path = open_file(lambda f: f, parts)
    dest_dirname, dest_basename = split(dest_path)

    with NamedTemporaryFile('w', prefix='.{}.'.format(dest_basename), dir=dest_dirname, delete=False) as partf:
        # Yield the file for writing
        with io.open(partf.fileno(), 'w', encoding=FILE_ENCODING, errors='xmlcharrefreplace', closefd=False) as f:
            yield f

        # NamedTemporaryFile is created 0600, set mode to the usual 0644
        os.fchmod(partf.fileno(), 0o644)

        # Flush buffers and sync the inode
        partf.flush()
        os.fsync(partf)  # type: ignore

        pfname = partf.name

    # Move to final destination
    if PY3:
        os.replace(pfname, dest_path)
    else:
        if os.name == 'nt':
            try_unlink(dest_path)  # Avoid potential FileExistsError
        os.rename(pfname, dest_path)


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


def set_period(period):
    """Prepare the period start and end timestamps"""
    i = 0
    tm = [int(period[:4]), 1, 1, 0, 0, 0, 0, 0, -1]
    if len(period) >= 6:
        i = 1
        tm[1] = int(period[4:6])
    if len(period) == 8:
        i = 2
        tm[2] = int(period[6:8])

    def mktime(tml):
        tmt = tuple(tml)  # type: Any
        return time.mktime(tmt)

    p_start = int(mktime(tm))
    tm[i] += 1
    p_stop = int(mktime(tm))
    return [p_start, p_stop]


class ApiParser(object):
    TRY_LIMIT = 2
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
        if options.reuse_json:
            prev_archive = save_folder
        elif prev_archive is None:
            return

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

        try:
            doc, status, reason = self._get_resp(base, params, headers)
        except (EnvironmentError, HTTPError) as e:
            log('URL is {}?{}\n[FATAL] Error retrieving API repsonse: {}\n'.format(base, urlencode(params), e))
            return None

        if not 200 <= status < 300:
            # Detect dashboard-only blogs by the error codes
            if status == 404 and doc is not None and self.dashboard_only_blog is None:
                errors = doc.get('errors', ())
                if len(errors) == 1 and errors[0].get('code') == 4012:
                    self.dashboard_only_blog = True
                    log('Found dashboard-only blog, trying svc API\n', account=True)
                    return self.apiparse(count, start)  # Recurse once
            log('URL is {}?{}\n[FATAL] {} API repsonse: HTTP {} {}\n{}'.format(
                base, urlencode(params),
                'Error retrieving' if doc is None else 'Non-OK',
                status, reason,
                '' if doc is None else '{}\n'.format(doc),
            ))
            if status == 401 and self.dashboard_only_blog:
                log("This is a dashboard-only blog, so you probably don't have the right cookies.{}\n".format(
                    '' if options.cookiefile else ' Try --cookiefile.',
                ))
            return None
        if doc is None:
            return None  # OK status but invalid JSON
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
        try_count = 0
        while True:
            try:
                with self.session.get(base, params=params, headers=headers) as resp:
                    try_count += 1
                    doc = None
                    ctype = resp.headers.get('Content-Type')
                    if not (200 <= resp.status_code < 300 or 400 <= resp.status_code < 500):
                        pass  # Server error, will not attempt to read body
                    elif ctype and ctype.split(';', 1)[0].strip() != 'application/json':
                        log("Unexpected Content-Type: '{}'\n".format(ctype))
                    else:
                        try:
                            doc = resp.json()
                        except ValueError as e:
                            log('{}: {}\n{} {} {}\n{!r}\n'.format(
                                e.__class__.__name__, e, resp.status_code, resp.reason, ctype,
                                resp.content.decode('utf-8'),
                            ))
                    status = resp.status_code if doc is None else doc['meta']['status']
                    if status == 429 and try_count < self.TRY_LIMIT and self._ratelimit_sleep(resp.headers):
                        continue
                    return doc, status, resp.reason if doc is None else httplib.responses.get(status, '(unknown)')
            except HTTPError:
                if not is_dns_working(timeout=5):
                    no_internet.signal()
                    continue
                raise

    @staticmethod
    def _ratelimit_sleep(headers):
        # Daily ratelimit
        if headers.get('X-Ratelimit-Perday-Remaining') == '0':
            reset = headers.get('X-Ratelimit-Perday-Reset')
            try:
                freset = float(reset)  # type: Optional[float]
            except ValueError:
                log("Expected numerical X-Ratelimit-Perday-Reset, got '{}'\n".format(reset))
                freset = None
            if freset is not None:
                treset = datetime.now() + timedelta(seconds=freset)
            raise RuntimeError('{}: Daily API ratelimit exceeded. Resume with --continue after reset {}.\n'.format(
                log.backup_account, 'sometime tomorrow' if freset is None else 'at {}'.format(treset.ctime())
            ))

        # Hourly ratelimit
        reset = headers.get('X-Ratelimit-Perhour-Reset')
        if reset is None:
            return False

        try:
            sleep_dur = float(reset)
        except ValueError:
            log("Expected numerical X-Ratelimit-Perhour-Reset, got '{}'\n".format(reset), account=True)
            return False

        hours, remainder = divmod(abs(sleep_dur), 3600)
        minutes, seconds = divmod(remainder, 60)
        sleep_dur_str = ' '.join(str(int(t[0])) + t[1] for t in ((hours, 'h'), (minutes, 'm'), (seconds, 's')) if t[0])

        if sleep_dur < 0:
            log('Warning: X-Ratelimit-Perhour-Reset is {} in the past\n'.format(sleep_dur_str), account=True)
            return True
        if sleep_dur > 3600:
            treset = datetime.now() + timedelta(seconds=sleep_dur)
            raise RuntimeError('{}: Refusing to sleep for {}. Resume with --continue at {}.'.format(
                log.backup_account, sleep_dur_str, treset.ctime(),
            ))

        log('Hit hourly ratelimit, sleeping for {} as requested\n'.format(sleep_dur_str), account=True)
        time.sleep(sleep_dur + 1)  # +1 to be sure we're past the reset
        return True


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
        if options.no_get:
            return  # We don't care if we don't have it

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
        if cpy_res or options.no_get:
            return  # We got the style or we don't care

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
def maybe_copy_media(prev_archive, path_parts, pa_path_parts=None):
    if prev_archive is None:
        return False  # Source does not exist
    if pa_path_parts is None:
        pa_path_parts = path_parts  # Default

    srcpath = join(prev_archive, *pa_path_parts)
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


def check_optional_modules():
    if options.exif:
        if pyexiv2 is None:
            raise RuntimeError("--exif: module 'pyexiv2' is not installed")
        if not hasattr(pyexiv2, 'ImageMetadata'):
            raise RuntimeError("--exif: module 'pyexiv2' is missing features, perhaps you need 'py3exiv2'?")
    if options.filter is not None and pyjq is None:
        raise RuntimeError("--filter: module 'pyjq' is not installed")
    if options.prev_archives and scandir is None:
        raise RuntimeError("--prev-archives: Python is less than 3.5 and module 'scandir' is not installed")


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

            with arch as archf:
                archf.write('\n'.join(archive))

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
        self.fatal_errors = False
        self.total_count = 0
        self.post_count = 0
        self.filter_skipped = 0
        self.title = None  # type: Optional[Text]
        self.subtitle = None  # type: Optional[str]
        self.pa_options = None  # type: Optional[JSONDict]

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
    def get_post_timestamps(posts, reason):
        BeautifulSoup = load_bs4(reason)
        for post in posts:
            with io.open(post, encoding=FILE_ENCODING) as pf:
                soup = BeautifulSoup(pf, 'lxml')
            postdate = soup.find('time')['datetime']
            del soup
            # No datetime.fromisoformat or datetime.timestamp on Python 2
            yield (datetime.strptime(postdate, '%Y-%m-%dT%H:%M:%SZ') - datetime(1970, 1, 1)) // timedelta(seconds=1)

    @classmethod
    def process_existing_backup(cls, account, prev_archive):
        complete_backup = os.path.exists(path_to('.complete'))
        if options.resume and complete_backup:
            raise RuntimeError('{}: Cannot continue complete backup'.format(account))
        try:
            with io.open(path_to('.first_run_options'), encoding=FILE_ENCODING) as f:
                first_run_options = json.load(f)
        except EnvironmentError as e:
            if getattr(e, 'errno', None) != errno.ENOENT:
                raise
            first_run_options = None

        class Options(object):
            def __init__(self, fro): self.fro = fro
            def differs(self, opt): return opt not in self.fro or orig_options[opt] != self.fro[opt]
            def first(self, opts): return {opt: self.fro.get(opt, '<not present>') for opt in opts}
            @staticmethod
            def this(opts): return {opt: orig_options[opt] for opt in opts}

        # These options must always match
        if first_run_options is not None:
            opts = Options(first_run_options)
            mustmatchdiff = tuple(filter(opts.differs, MUST_MATCH_OPTIONS))
            if mustmatchdiff:
                raise RuntimeError('{}: The script was given {} but the existing backup was made with {}'.format(
                    account, opts.this(mustmatchdiff), opts.first(mustmatchdiff)))

            backdiff = tuple(filter(opts.differs, BACKUP_CHANGING_OPTIONS))
            if options.resume:
                backdiff_nondef = tuple(opt for opt in backdiff if orig_options[opt] != parser.get_default(opt))
                if backdiff_nondef and not options.ignore_diffopt:
                    raise RuntimeError('{}: The script was given {} but the existing backup was made with {}. You may '
                                       'skip this check with --ignore-diffopt.'.format(
                                            account, opts.this(backdiff_nondef), opts.first(backdiff_nondef)))
            elif complete_backup:
                pass  # Complete archives may be added to with different options
            elif not backdiff:
                raise RuntimeError('{}: Found incomplete archive, try --continue'.format(account))
            elif not options.ignore_diffopt:
                raise RuntimeError('{}: Refusing to make a different backup (with {} instead of {}) over an incomplete '
                                   'archive. Delete the old backup to start fresh, or skip this check with '
                                   '--ignore-diffopt (optionally with --continue).'.format(
                                       account, opts.this(backdiff), opts.first(backdiff)))

        pa_options = None
        if prev_archive is not None:
            try:
                with io.open(join(prev_archive, '.first_run_options'), encoding=FILE_ENCODING) as f:
                    pa_options = json.load(f)
            except EnvironmentError as e:
                if getattr(e, 'errno', None) != errno.ENOENT:
                    raise
                pa_options = None

            # These options must always match
            if pa_options is not None:
                pa_opts = Options(pa_options)
                mustmatchdiff = tuple(filter(pa_opts.differs, PREV_MUST_MATCH_OPTIONS))
                if mustmatchdiff:
                    raise RuntimeError('{}: The script was given {} but the previous archive was made with {}'.format(
                        account, pa_opts.this(mustmatchdiff), pa_opts.first(mustmatchdiff)))

        oldest_tstamp = None
        if not complete_backup:
            # Read every post to find the oldest timestamp already saved
            filter_ = join('*', dir_index) if options.dirs else '*' + post_ext
            post_glob = glob(path_to(post_dir, filter_))
            if not options.resume:
                pass  # No timestamp needed but may want to know if posts are present
            elif not post_glob:
                raise RuntimeError('{}: Cannot continue empty backup'.format(account))
            else:
                log('Found incomplete backup. Finding oldest post (may take a while)\n', account=True)
                oldest_tstamp = min(cls.get_post_timestamps(post_glob, 'continue incomplete backup'))
                log(
                    'Backing up posts before timestamp={} ({})\n'.format(oldest_tstamp, time.ctime(oldest_tstamp)),
                    account=True,
                )

        write_fro = False
        if first_run_options is not None and options.resume:
            # Load saved options, unless they were overridden with --ignore-diffopt
            for opt in BACKUP_CHANGING_OPTIONS:
                if opt not in backdiff_nondef:
                    setattr(options, opt, first_run_options[opt])
        else:
            # Load original options
            for opt in BACKUP_CHANGING_OPTIONS:
                setattr(options, opt, orig_options[opt])
            if first_run_options is None and not (complete_backup or post_glob):
                # Presumably this is the initial backup of this blog
                write_fro = True

        if pa_options is None and prev_archive is not None:
            # Fallback assumptions
            log('Warning: Unknown media path options for previous archive, assuming they match ours\n', account=True)
            pa_options = {opt: getattr(options, opt) for opt in MEDIA_PATH_OPTIONS}

        return oldest_tstamp, pa_options, write_fro

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
            post_class = TumblrPost
            have_custom_css = os.access(path_to(custom_css), os.R_OK)

        self.post_count = 0
        self.filter_skipped = 0

        oldest_tstamp, self.pa_options, write_fro = self.process_existing_backup(account, prev_archive)
        check_optional_modules()

        if options.incremental or options.resume:
            filter_ = join('*', dir_index) if options.dirs else '*' + post_ext
            post_glob = glob(path_to(post_dir, filter_))

        ident_max = None
        if options.incremental and post_glob:
            if options.likes:
                # Read every post to find the newest timestamp already saved
                log('Finding newest liked post (may take a while)\n', account=True)
                ident_max = max(self.get_post_timestamps(post_glob, 'backup likes incrementally'))
                log('Backing up posts after timestamp={} ({})\n'.format(ident_max, time.ctime(ident_max)), account=True)
            else:
                # Get the highest post id already saved
                ident_max = max(long(splitext(split(f)[1])[0]) for f in post_glob)
                log('Backing up posts after id={}\n'.format(ident_max), account=True)

        if options.resume:
            # Update skip and count based on where we left off
            options.skip = 0
            self.post_count = len(post_glob)

        log.status('Getting basic information\r')

        api_parser = ApiParser(base, account)
        api_thread = AsyncCallable(main_thread_lock, api_parser.apiparse, 'API Thread')
        with api_thread.response.mutex:
            api_parser.read_archive(prev_archive)
            api_thread.put(1)
            resp = api_thread.get()
        if not resp:
            self.fatal_errors = self.errors = True
            return

        # collect all the meta information
        if options.likes:
            if not resp.get('blog', {}).get('share_likes', True):
                print('{} does not have public likes\n'.format(account))
                self.fatal_errors = self.errors = True
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

        if write_fro:
            # Blog directory gets created here
            with open_text('.first_run_options') as f:
                f.write(to_unicode(json.dumps(orig_options)))

        def build_index():
            log.status('Getting avatar and style\r')
            get_avatar(prev_archive)
            get_style(prev_archive)
            if not have_custom_css:
                save_style()
            log.status('Building index\r')
            ix = Indices(self)
            ix.build_index()
            ix.save_index()

            if not (self.fatal_errors or os.path.exists(path_to('.complete'))):
                # Make .complete file
                sf = opendir(save_folder, os.O_RDONLY)
                try:
                    os.fdatasync(sf)
                    with io.open(open_file(lambda f: f, ('.complete',)), 'wb') as f:
                        os.fsync(f)  # type: ignore
                    os.fdatasync(sf)
                finally:
                    os.close(sf)

        if options.count == 0:
            build_index()
            return

        # use the meta information to create a HTML header
        TumblrPost.post_header = self.header(body_class='post')

        jq_filter = None if options.filter is None else pyjq.compile(options.filter)  # pytype: disable=attribute-error
        request_sets = None if options.request is None else {typ: set(tags) for typ, tags in options.request.items()}

        # start the thread pool
        backup_pool = ThreadPool()

        before = options.period[1] if options.period else None
        if oldest_tstamp is not None:
            before = oldest_tstamp if before is None else min(before, oldest_tstamp)

        # returns whether any posts from this batch were saved
        def _backup(posts, post_respfiles):
            def sort_key(x): return x[0]['liked_timestamp'] if options.likes else long(x[0]['id'])
            sorted_posts = sorted(zip(posts, post_respfiles), key=sort_key, reverse=True)
            for p, prf in sorted_posts:
                no_internet.check()
                post = post_class(p, account, prf, prev_archive, self.pa_options)
                if before is not None and post.date >= before:
                    raise RuntimeError('Found post with date ({}) newer than before param ({})'.format(
                        post.date, before))
                if ident_max is None:
                    pass  # No limit
                elif (p['liked_timestamp'] if options.likes else long(post.ident)) <= ident_max:
                    log('Stopping backup: Incremental backup complete\n', account=True)
                    return False
                if options.period and post.date < options.period[0]:
                    log('Stopping backup: Reached end of period\n', account=True)
                    return False
                if request_sets:
                    if post.typ not in request_sets:
                        continue
                    tags = request_sets[post.typ]
                    if not (TAG_ANY in tags or tags & {t.lower() for t in post.tags}):
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
                if jq_filter and not jq_filter.first(p):
                    self.filter_skipped += 1
                    continue

                with multicond:
                    while backup_pool.queue.qsize() >= backup_pool.queue.maxsize:
                        no_internet.check(release=True)
                        # All conditions false, wait for a change
                        multicond.wait((backup_pool.queue.not_full, no_internet.cond))
                    backup_pool.add_work(post.save_post)

                self.post_count += 1
                if options.count and self.post_count >= options.count:
                    log('Stopping backup: Reached limit of {} posts\n'.format(options.count), account=True)
                    return False
            return True

        try:
            # Get the JSON entries from the API, which we can only do for MAX_POSTS posts at once.
            # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
            i = options.skip

            while True:
                # find the upper bound
                log.status('Getting {}posts {} to {}{}\r'.format(
                    'liked ' if options.likes else '', i, i + MAX_POSTS - 1,
                    '' if count_estimate is None else ' (of {} expected)'.format(count_estimate),
                ))

                with multicond:
                    api_thread.put(MAX_POSTS, i, before)

                    while not api_thread.response.qsize():
                        no_internet.check(release=True)
                        # All conditions false, wait for a change
                        multicond.wait((api_thread.response.not_empty, no_internet.cond))

                    resp = api_thread.get(block=False)

                if resp is None:
                    self.fatal_errors = self.errors = True
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

            api_thread.quit()
            backup_pool.wait()  # wait until all posts have been saved
        except:
            api_thread.quit()
            backup_pool.cancel()  # ensure proper thread pool termination
            raise

        if backup_pool.errors:
            self.errors = True

        # postprocessing
        if not options.blosxom and self.post_count:
            build_index()

        log.status(None)
        skipped_msg = (', {} did not match filter'.format(self.filter_skipped)) if self.filter_skipped else ''
        log(
            '{} {}posts backed up{}\n'.format(self.post_count, 'liked ' if options.likes else '', skipped_msg),
            account=True,
        )
        self.total_count += self.post_count


class TumblrPost(object):
    post_header = ''  # set by TumblrBackup.backup()

    def __init__(self, post, backup_account, respfile, prev_archive, pa_options):
        # type: (JSONDict, str, Text, Text, Optional[JSONDict]) -> None
        self.post = post
        self.backup_account = backup_account
        self.respfile = respfile
        self.prev_archive = prev_archive
        self.pa_options = pa_options
        self.creator = post.get('blog_name') or post['tumblelog']
        self.ident = str(post['id'])
        self.url = post['post_url']
        self.shorturl = post['short_url']
        self.typ = str(post['type'])
        self.date = post['liked_timestamp' if options.likes else 'timestamp']  # type: float
        self.isodate = datetime.utcfromtimestamp(self.date).isoformat() + 'Z'
        self.tm = time.localtime(self.date)
        self.title = u''
        self.tags = post['tags']  # type: Text
        self.note_count = post.get('note_count')
        if self.note_count is None:
            self.note_count = post.get('notes', {}).get('count')
        if self.note_count is None:
            self.note_count = 0
        self.reblogged_from = post.get('reblogged_from_url')
        self.reblogged_root = post.get('reblogged_root_url')
        self.source_title = post.get('source_title', '')
        self.source_url = post.get('source_url', '')
        self.file_name = join(self.ident, dir_index) if options.dirs else self.ident + post_ext
        self.llink = self.ident if options.dirs else self.file_name
        self.media_dir = join(post_dir, self.ident) if options.dirs else media_dir
        self.media_url = urlpathjoin(save_dir, self.media_dir)
        self.media_folder = path_to(self.media_dir)

    def get_content(self):
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

        content_str = '\n'.join(content)

        # fix wrongly nested HTML elements
        for p in ('<p>(<({})>)', '(</({})>)</p>'):
            content_str = re.sub(p.format('p|ol|iframe[^>]*'), r'\1', content_str)

        return content_str

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
        try:
            import youtube_dl
            from youtube_dl.utils import sanitize_filename
        except ImportError:
            raise RuntimeError("--save-video: module 'youtube_dl' is not installed")
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
        saved_name = self.download_media(media_url, extension=extension)
        if saved_name is not None:
            return urlpathjoin(self.media_url, saved_name)
        return media_url

    def get_image_url(self, image_url, offset):
        """Saves an image if not saved yet. Returns the new URL or
        the original URL in case of download errors."""
        saved_name = self.download_media(image_url, offset='_o%s' % offset if offset else '')
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
        saved_name = self.download_media(image_url, filename=image_filename)
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
        saved_name = self.download_media(poster_url, filename=poster_filename)
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

    def get_filename(self, url_path, image_names, offset=''):
        """Determine the image file name depending on options.image_names"""
        fname = urlbasename(url_path)
        ext = urlsplitext(fname)[1]
        if options.image_names == 'i':
            return self.ident + offset + ext
        if options.image_names == 'bi':
            return self.backup_account + '_' + self.ident + offset + ext
        # delete characters not allowed under Windows
        return re.sub(r'[:<>"/\\|*?]', '', fname) if os.name == 'nt' else fname

    def download_media(self, url, filename=None, offset='', extension=None):
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

        def get_path(media_dir, image_names, hostdirs):
            if filename is not None:
                fname = filename
            else:
                fname = self.get_filename(parsed_url.path, image_names, offset)
                if extension is not None:
                    fname = splitext(fname)[0] + extension
            parts = (media_dir,) + ((hostdir,) if hostdirs else ()) + (fname,)
            return parts

        path_parts = get_path(self.media_dir, options.image_names, options.hostdirs)

        if self.prev_archive is None:
            cpy_res = False
        else:
            assert self.pa_options is not None
            pa_path_parts = get_path(
                join(post_dir, self.ident) if self.pa_options['dirs'] else media_dir,
                self.pa_options['image_names'], self.pa_options['hostdirs'],
            )
            cpy_res = maybe_copy_media(self.prev_archive, path_parts, pa_path_parts)
        if not cpy_res and not options.no_get:
            # We don't have the media and we want it
            try:
                wget_retrieve(url, open_file(lambda f: f, path_parts))
            except WGError as e:
                e.log()
                return None

        return path_parts[-1]

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
        post += self.get_content()
        foot = []
        if self.tags:
            foot.append(u''.join(self.tag_link(t) for t in self.tags))
        if self.source_title and self.source_url:
            foot.append(u'<a title=Source href=%s>%s</a>' %
                (self.source_url, self.source_title)
            )

        notes_html = u''

        if options.save_notes or options.copy_notes:
            BeautifulSoup = load_bs4('save notes' if options.save_notes else 'copy notes')

        if options.copy_notes:
            # Copy notes from prev_archive
            with io.open(join(self.prev_archive, post_dir, self.ident + post_ext)) as post_file:
                soup = BeautifulSoup(post_file, 'lxml')
            notes = soup.find('ol', class_='notes')
            if notes is not None:
                notes_html = u''.join([n.prettify() for n in notes.find_all('li')])

        if options.save_notes and self.backup_account not in disable_note_scraper and not notes_html.strip():
            import note_scraper

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
        if options.json and not options.reuse_json:
            with open_text(json_dir, self.ident + '.json') as f:
                f.write(self.get_json_content())
        path_parts = self.get_path()
        try:
            with open_text(*path_parts) as f:
                f.write(self.get_post())
            os.utime(path_to(*path_parts), (self.date, self.date))
        except Exception:
            print('Caught exception while saving post {}:'.format(self.ident), file=sys.stderr)
            traceback.print_exc()
            return False
        return True

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
        post += '\n\n' + self.get_content()
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
    def __init__(self, max_queue=1000):
        self.queue = LockedQueue(main_thread_lock, max_queue)  # type: LockedQueue[Callable[[], None]]
        self.quit = threading.Condition(main_thread_lock)
        self.quit_flag = False
        self.abort_flag = False
        self.errors = False
        self.stoplock = threading.Lock()
        self.threads = [WorkerThread(self.stoplock, target=self.handler) for _ in range(options.threads)]
        for t in self.threads:
            t.start()

    def add_work(self, *args, **kwargs):
        self.queue.put(*args, **kwargs)

    def wait(self):
        with multicond:
            log.status('{} remaining posts to save\r'.format(self.queue.qsize()))
            self.quit_flag = True
            self.quit.notify_all()
            while self.queue.unfinished_tasks:
                no_internet.check(release=True)
                # All conditions false, wait for a change
                multicond.wait((self.queue.all_tasks_done, no_internet.cond))

    def cancel(self):
        with main_thread_lock:
            self.abort_flag = True
            self.quit.notify_all()
            no_internet.destroy()

        duh = disable_unraisable_hook() if hasattr(signal, 'pthread_kill') else nullcontext()  # type: Any
        with duh:
            # The SIGTERM handler raises SystemExit to gracefully stop the worker
            # threads. Otherwise, we must wait for them to finish their posts.
            if hasattr(signal, 'pthread_kill'):
                for thread in self.threads:
                    with self.stoplock:
                        if thread.ident is None or not thread.alive:
                            continue
                        try:
                            signal.pthread_kill(thread.ident, signal.SIGTERM)  # type: ignore[attr-defined]
                        except EnvironmentError as e:
                            if getattr(e, 'errno', None) == errno.ESRCH:
                                continue  # Ignore ESRCH errors
                            raise

            for i, t in enumerate(self.threads, start=1):
                log.status('Stopping threads {}{}\r'.format(' ' * i, '.' * (len(self.threads) - i)))
                t.join()

        with main_thread_lock:
            self.queue.queue.clear()
            self.queue.all_tasks_done.notify_all()

    def handler(self):
        def wait_for_work():
            while not self.abort_flag:
                if self.queue.qsize():
                    return True
                elif self.quit_flag:
                    break
                # All conditions false, wait for a change
                multicond.wait((self.queue.not_empty, self.quit))
            return False

        while True:
            with multicond:
                if not wait_for_work():
                    break
                work = self.queue.get(block=False)
                qsize = self.queue.qsize()
                if self.quit_flag and qsize % REM_POST_INC == 0:
                    log.status('{} remaining posts to save\r'.format(qsize))

            try:
                success = work()
            finally:
                self.queue.task_done()
            if not success:
                self.errors = True


class WorkerThread(threading.Thread):
    def __init__(self, stoplock, **kwargs):
        super(WorkerThread, self).__init__(**kwargs)
        self.stoplock = stoplock
        self.alive = False

    def run(self):
        self.alive = True
        try:
            super(WorkerThread, self).run()
        finally:
            with self.stoplock:
                self.alive = False


if __name__ == '__main__':
    # The default of 'fork' can cause deadlocks, even on Linux
    # See https://bugs.python.org/issue40399
    if not PY3:
        pass  # No set_start_method. Here be dragons
    elif 'forkserver' in multiprocessing.get_all_start_methods():
        multiprocessing.set_start_method('forkserver')  # Fastest safe option, if supported
    else:
        multiprocessing.set_start_method('spawn')  # Slow but safe

    # Raises SystemExit to terminate gracefully
    def handle_term_signal(signum, frame):
        if sys.exc_info() != (None, None, None):
            return  # Not a good time to exit
        if not PY3:
            pass  # No is_finalizing
        elif sys.is_finalizing():
            return  # Not a good time to exit
        sys.exit(1)
    signal.signal(signal.SIGTERM, handle_term_signal)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, handle_term_signal)

    no_internet.setup(main_thread_lock)

    import argparse

    class CSVCallback(argparse.Action):
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
                    if not parts:
                        request[typ] = [TAG_ANY]
                        continue
                    if typ not in request:
                        request[typ] = []
                    request[typ].extend(parts)
            setattr(namespace, self.dest, request)

    class TagsCallback(RequestCallback):
        def __call__(self, parser, namespace, values, option_string=None):
            super(TagsCallback, self).__call__(
                parser, namespace, TYPE_ANY + ':' + values.replace(',', ':'), option_string,
            )

    class PeriodCallback(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            try:
                pformat = {'y': '%Y', 'm': '%Y%m', 'd': '%Y%m%d'}[values]
            except KeyError:
                period = values.replace('-', '')
                if not re.match(r'^\d{4}(\d\d)?(\d\d)?$', period):
                    parser.error("Period must be 'y', 'm', 'd' or YYYY[MM[DD]]")
            else:
                period = time.strftime(pformat)
            setattr(namespace, self.dest, set_period(period))

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
    parser.add_argument('-p', '--period', action=PeriodCallback,
                        help="limit the backup to PERIOD ('y', 'm', 'd' or YYYY[MM[DD]])")
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
    parser.add_argument('-e', '--exif', action=CSVCallback, default=[], metavar='KW',
                        help='add EXIF keyword tags to each picture'
                             " (comma-separated values; '-' to remove all tags, '' to add no extra tags)")
    parser.add_argument('-S', '--no-ssl-verify', action='store_true', help='ignore SSL verification errors')
    parser.add_argument('--prev-archives', action=CSVCallback, default=[], metavar='DIRS',
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
    parser.add_argument('--threads', type=int, default=20, help='number of threads to use for post retrieval')
    parser.add_argument('--continue', action='store_true', dest='resume', help='Continue an incomplete first backup')
    parser.add_argument('--ignore-diffopt', action='store_true',
                        help='Force backup over an incomplete archive with different options')
    parser.add_argument('--no-get', action='store_true', help="Don't retrieve files not found in --prev-archives")
    parser.add_argument('--reuse-json', action='store_true', help='Reuse the API responses saved with --json')
    parser.add_argument('blogs', nargs='*')
    options = parser.parse_args()
    blogs = options.blogs or DEFAULT_BLOGS
    del options.blogs
    orig_options = vars(options).copy()

    if not blogs:
        parser.error('Missing blog-name')
    if sum(1 for arg in ('resume', 'incremental', 'auto') if getattr(options, arg) not in (None, False)) > 1:
        parser.error('Only one of --continue, --incremental, or --auto may be given')
    if options.auto is not None and options.auto != time.localtime().tm_hour:
        options.incremental = True
    if options.resume or options.incremental:
        # Do not clobber or count posts that were already backed up
        options.no_post_clobber = True
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
    if options.cookiefile is not None and not os.access(options.cookiefile, os.R_OK):
        parser.error('--cookiefile: file cannot be read')
    if options.copy_notes and not options.prev_archives:
        parser.error('--copy-notes requires --prev-archives')
    if options.notes_limit is not None:
        if not options.save_notes:
            parser.error('--notes-limit requires --save-notes')
        if options.notes_limit < 1:
            parser.error('--notes-limit: Value must be at least 1')
    if options.prev_archives and options.reuse_json:
        parser.error('--prev-archives and --reuse-json are mutually exclusive')
    if options.prev_archives:
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
    if options.threads < 1:
        parser.error('--threads: must use at least one thread')
    if options.no_get and not options.prev_archives:
        parser.error('--no-get requires --prev-archives')
    if options.no_get and options.save_notes:
        print('Warning: --save-notes uses HTTP regardless of --no-get', file=sys.stderr)

    check_optional_modules()

    if not API_KEY:
        sys.stderr.write('''\
Missing API_KEY; please get your own API key at
https://www.tumblr.com/oauth/apps\n''')
        sys.exit(1)

    wget_retrieve = WgetRetrieveWrapper(options, log)
    setup_wget(not options.no_ssl_verify, options.user_agent)

    ApiParser.setup()
    tb = TumblrBackup()
    try:
        for i, account in enumerate(blogs):
            log.backup_account = account
            tb.backup(account, options.prev_archives[i] if options.prev_archives else None)
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPT)

    sys.exit(tb.exit_code())
