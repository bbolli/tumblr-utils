# builtin modules
import argparse
import calendar
import contextlib
import errno
import hashlib
import http.client
import itertools
import json
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
from multiprocessing.queues import SimpleQueue
from os.path import join, split, splitext
from pathlib import Path
from posixpath import basename as urlbasename, join as urlpathjoin, splitext as urlsplitext
from tempfile import NamedTemporaryFile
from types import ModuleType
from typing import (TYPE_CHECKING, Any, Callable, ContextManager, DefaultDict, Dict, Iterable, Iterator, List, Optional,
                    Set, TextIO, Tuple, Type, Union, cast)
from urllib.parse import quote, urlencode, urlparse
from xml.sax.saxutils import escape

# third-party modules
import filetype
import platformdirs
import requests

# internal modules
from .util import (AsyncCallable, ConnectionFile, FakeGenericMeta, LockedQueue, LogLevel, MultiCondition, copyfile,
                   enospc, fdatasync, fsync, have_module, is_dns_working, make_requests_session, no_internet, opendir,
                   to_bytes)
from .wget import HTTPError, HTTP_TIMEOUT, Retry, WGError, WgetRetrieveWrapper, setup_wget, touch, urlopen
from .is_reblog import post_is_reblog

if TYPE_CHECKING:
    from typing_extensions import Literal
    from bs4 import Tag
else:
    class Literal(metaclass=FakeGenericMeta):
        pass
    Tag = None

JSONDict = Dict[str, Any]

# extra optional packages
try:
    import pyexiv2
except ImportError:
    if not TYPE_CHECKING:
        pyexiv2 = None

try:
    import jq
except ImportError:
    if not TYPE_CHECKING:
        jq = None

# Imported later if needed
ytdl_module: Optional[ModuleType] = None

# Format of displayed tags
TAG_FMT = '#{}'

# Format of tag link URLs; set to None to suppress the links.
# Named placeholders that will be replaced: domain, tag
TAGLINK_FMT = 'https://{domain}/tagged/{tag}'

# exit codes
EXIT_SUCCESS    = 0
EXIT_FAILURE    = 1
# EXIT_ARGPARSE = 2 -- returned by argparse
EXIT_INTERRUPT  = 3
EXIT_ERRORS     = 4
EXIT_NOPOSTS    = 5

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
HTTP_RETRY.RETRY_AFTER_STATUS_CODES = frozenset((413,))  # type: ignore[misc]

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass
FILE_ENCODING = 'utf-8'

PREV_MUST_MATCH_OPTIONS = ('likes', 'blosxom')
MEDIA_PATH_OPTIONS = ('dirs', 'hostdirs', 'image_names')
MUST_MATCH_OPTIONS = PREV_MUST_MATCH_OPTIONS + MEDIA_PATH_OPTIONS
BACKUP_CHANGING_OPTIONS = (
    'save_images', 'save_video', 'save_video_tumblr', 'save_audio', 'save_notes', 'copy_notes', 'notes_limit', 'json',
    'count', 'skip', 'period', 'request', 'filter', 'no_reblog', 'only_reblog', 'exif', 'prev_archives',
    'use_server_timestamps', 'user_agent', 'no_get', 'internet_archive', 'media_list', 'idents',
)

parser: argparse.ArgumentParser
options: argparse.Namespace
orig_options: Dict[str, Any]
API_KEY: str
wget_retrieve: Optional[WgetRetrieveWrapper] = None
main_thread_lock = threading.RLock()
multicond = MultiCondition(main_thread_lock)
disable_note_scraper: Set[str] = set()
disablens_lock = threading.Lock()
downloading_media: Set[str] = set()
downloading_media_cond = threading.Condition()


def load_bs4(reason):
    sys.modules['soupsieve'] = ()  # type: ignore[assignment]
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("Cannot {} without module 'bs4'".format(reason))
    try:
        import lxml  # noqa: F401
    except ImportError:
        raise RuntimeError("Cannot {} without module 'lxml'".format(reason))
    return BeautifulSoup


class Logger:
    def __init__(self):
        self.lock = threading.Lock()
        self.backup_account: Optional[str] = None
        self.status_msg: Optional[str] = None

    def log(self, level: LogLevel, msg: str, account: bool = False) -> None:
        if options.quiet and level < LogLevel.WARN:
            return
        with self.lock:
            for line in msg.splitlines(True):
                self._print(line, account)
            if self.status_msg:
                self._print(self.status_msg, account=True)
            sys.stdout.flush()

    def info(self, msg, account=False):
        self.log(LogLevel.INFO, msg, account)

    def warn(self, msg, account=False):
        self.log(LogLevel.WARN, msg, account)

    def error(self, msg, account=False):
        self.log(LogLevel.ERROR, msg, account)

    def status(self, msg):
        self.status_msg = msg
        self.log(LogLevel.INFO, '')

    def _print(self, msg, account=False):
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
        print(msg + pad + term, end='', file=sys.stderr if options.json_info else sys.stdout)


logger = Logger()


def mkdir(dir, recursive=False):
    if not os.path.exists(dir):
        try:
            if recursive:
                os.makedirs(dir)
            else:
                os.mkdir(dir)
        except FileExistsError:
            pass  # ignored


def path_to(*parts):
    return join(save_folder, *parts)


def open_file(open_fn, parts):
    mkdir(path_to(*parts[:-1]), recursive=True)
    return open_fn(path_to(*parts))


class open_outfile:
    def __init__(self, mode, *parts, **kwargs):
        self._dest_path = open_file(lambda f: f, parts)
        dest_dirname, dest_basename = split(self._dest_path)

        self._partf = NamedTemporaryFile(mode, prefix='.{}.'.format(dest_basename), dir=dest_dirname, delete=False)
        # NB: open by name so name attribute is accurate
        self._f = open(self._partf.name, mode, **kwargs)

    def __enter__(self):
        return self._f

    def __exit__(self, exc_type, exc_value, tb):
        partf = self._partf
        self._f.close()

        if exc_type is not None:
            # roll back on exception; do not write partial files
            partf.close()
            os.unlink(partf.name)
            return

        # NamedTemporaryFile is created 0600, set mode to the usual 0644
        if os.name == 'posix':
            os.fchmod(partf.fileno(), 0o644)
        else:
            os.chmod(partf.name, 0o644)

        # Flush buffers and sync the inode
        partf.flush()
        fsync(partf)
        partf.close()

        # Move to final destination
        os.replace(partf.name, self._dest_path)


@contextlib.contextmanager
def open_text(*parts, mode='w') -> Iterator[TextIO]:
    assert 'b' not in mode
    with open_outfile(mode, *parts, encoding=FILE_ENCODING, errors='xmlcharrefreplace') as f:
        yield f


def strftime(fmt, t=None):
    if t is None:
        t = time.localtime()
    return time.strftime(fmt, t)


def get_api_url(account):
    """construct the tumblr API URL"""
    global blog_name
    blog_name = account
    if any(c in account for c in '/\\') or account in ('.', '..'):
        raise ValueError('Invalid blog name: {!r}'.format(account))
    if '.' not in account:
        blog_name += '.tumblr.com'
    return 'https://api.tumblr.com/v2/blog/%s/%s' % (
        blog_name, 'likes' if options.likes else 'posts'
    )


def parse_period_date(period):
    """Prepare the period start and end timestamps"""
    timefn: Callable[[Any], float] = time.mktime
    # UTC marker
    if period[-1] == 'Z':
        period = period[:-1]
        timefn = calendar.timegm

    i = 0
    tm = [int(period[:4]), 1, 1, 0, 0, 0, 0, 0, -1]
    if len(period) >= 6:
        i = 1
        tm[1] = int(period[4:6])
    if len(period) == 8:
        i = 2
        tm[2] = int(period[6:8])

    def mktime(tml):
        tmt: Any = tuple(tml)
        return timefn(tmt)

    p_start = int(mktime(tm))
    tm[i] += 1
    p_stop = int(mktime(tm))
    return [p_start, p_stop]


def get_posts_key() -> str:
    return 'liked_posts' if options.likes else 'posts'


class ApiParser:
    TRY_LIMIT = 2
    session: Optional[requests.Session] = None

    def __init__(self, base, account):
        self.base = base
        self.account = account
        self.prev_resps: Optional[List[str]] = None
        self.dashboard_only_blog: Optional[bool] = None
        self._prev_iter: Optional[Iterator[JSONDict]] = None
        self._last_mode: Optional[str] = None
        self._last_offset: Optional[int] = None

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
            return True

        def read_resp(path):
            with open(path, encoding=FILE_ENCODING) as jf:
                return json.load(jf)

        if options.likes:
            logger.warn('Reading liked timestamps from saved responses (may take a while)\n', account=True)

        if options.idents is None:
            respfiles: Iterable[str] = (
                e.path for e in os.scandir(join(prev_archive, 'json'))
                if e.name.endswith('.json') and e.is_file()
            )
        else:
            respfiles = []
            for ident in options.idents:
                resp = join(prev_archive, 'json', str(ident) + '.json')
                if not os.path.isfile(resp):
                    logger.error("post '{}' not found\n".format(ident), account=True)
                    return False
                respfiles.append(resp)

        self.prev_resps = sorted(
            respfiles,
            key=lambda p: (
                read_resp(p)['liked_timestamp'] if options.likes
                else int(os.path.basename(p)[:-5])
            ),
            reverse=True,
        )
        return True

    def _iter_prev(self) -> Iterator[JSONDict]:
        assert self.prev_resps is not None
        for path in self.prev_resps:
            with open(path, encoding=FILE_ENCODING) as f:
                try:
                    yield json.load(f)
                except ValueError as e:
                    f.seek(0)
                    logger.error('{}: {}\n{!r}\n'.format(e.__class__.__name__, e, f.read()))

    def get_initial(self) -> Optional[JSONDict]:
        if self.prev_resps is not None:
            try:
                first_post = next(self._iter_prev())
            except StopIteration:
                return None
            r = {get_posts_key(): [first_post], 'blog': first_post['blog'].copy()}
            if options.likes:
                r['liked_count'] = len(self.prev_resps)
            else:
                r['blog']['posts'] = len(self.prev_resps)
            return r

        resp = self.apiparse(1)
        if self.dashboard_only_blog and resp and resp['posts']:
            # svc API doesn't return blog info, steal it from the first post
            resp['blog'] = resp['posts'][0]['blog']
        return resp

    def apiparse(self, count, start=0, before=None, ident=None) -> Optional[JSONDict]:
        if self.prev_resps is not None:
            if self._prev_iter is None:
                self._prev_iter = self._iter_prev()
            if ident is not None:
                assert self._last_mode in (None, 'ident')
                self._last_mode = 'ident'
                # idents are pre-filtered
                try:
                    posts = [next(self._prev_iter)]
                except StopIteration:
                    return None
            else:
                it = self._prev_iter
                if before is not None:
                    assert self._last_mode in (None, 'before')
                    assert self._last_offset is None or before < self._last_offset
                    self._last_mode = 'before'
                    self._last_offset = before
                    it = itertools.dropwhile(
                        lambda p: p['liked_timestamp' if options.likes else 'timestamp'] >= before,
                        it,
                    )
                else:
                    assert self._last_mode in (None, 'offset')
                    assert start == (0 if self._last_offset is None else self._last_offset + MAX_POSTS)
                    self._last_mode = 'offset'
                    self._last_offset = start
                posts = list(itertools.islice(it, None, count))
            return {get_posts_key(): posts}

        if self.dashboard_only_blog:
            base = 'https://www.tumblr.com/svc/indash_blog'
            params = {'tumblelog_name_or_id': self.account, 'post_id': '', 'limit': count,
                      'should_bypass_safemode': 'true', 'should_bypass_tagfiltering': 'true'}
            headers: Optional[Dict[str, str]] = {
                'Referer': 'https://www.tumblr.com/dashboard/blog/' + self.account,
                'X-Requested-With': 'XMLHttpRequest',
            }
        else:
            base = self.base
            params = {'api_key': API_KEY, 'limit': count, 'reblog_info': 'true'}
            headers = None
        if ident is not None:
            params['post_id' if self.dashboard_only_blog else 'id'] = ident
        elif before is not None and not self.dashboard_only_blog:
            params['before'] = before
        elif start > 0:
            params['offset'] = start

        try:
            doc, status, reason = self._get_resp(base, params, headers)
        except (OSError, HTTPError) as e:
            logger.error('URL is {}?{}\n[FATAL] Error retrieving API repsonse: {!r}\n'.format(
                base, urlencode(params), e,
            ))
            return None

        if not 200 <= status < 300:
            # Detect dashboard-only blogs by the error codes
            if status == 404 and self.dashboard_only_blog is None and not (doc is None or options.likes):
                errors = doc.get('errors', ())
                if len(errors) == 1 and errors[0].get('code') == 4012:
                    self.dashboard_only_blog = True
                    logger.info('Found dashboard-only blog, trying svc API\n', account=True)
                    return self.apiparse(count, start)  # Recurse once
            if status == 403 and options.likes:
                logger.error('HTTP 403: Most likely {} does not have public likes.\n'.format(self.account))
                return None
            logger.error('URL is {}?{}\n[FATAL] {} API repsonse: HTTP {} {}\n{}'.format(
                base, urlencode(params),
                'Error retrieving' if doc is None else 'Non-OK',
                status, reason,
                '' if doc is None else '{}\n'.format(doc),
            ))
            if status == 401 and self.dashboard_only_blog:
                logger.error("This is a dashboard-only blog, so you probably don't have the right cookies.{}\n".format(
                    '' if options.cookiefile else ' Try --cookiefile.',
                ))
            return None
        if doc is None:
            return None  # OK status but invalid JSON

        if self.dashboard_only_blog:
            with disablens_lock:
                if self.account not in disable_note_scraper:
                    disable_note_scraper.add(self.account)
                    logger.info('[Note Scraper] Dashboard-only blog - scraping disabled for {}\n'.format(self.account))
        elif self.dashboard_only_blog is None:
            # If the first API request succeeds, it's a public blog
            self.dashboard_only_blog = False

        return doc.get('response')

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
                        logger.error("Unexpected Content-Type: '{}'\n".format(ctype))
                    else:
                        try:
                            doc = resp.json()
                        except ValueError as e:
                            logger.error('{}: {}\n{} {} {}\n{!r}\n'.format(
                                e.__class__.__name__, e, resp.status_code, resp.reason, ctype,
                                resp.content.decode('utf-8'),
                            ))
                    status = resp.status_code if doc is None else doc['meta']['status']
                    if status == 429 and try_count < self.TRY_LIMIT and self._ratelimit_sleep(resp.headers):
                        continue
                    return doc, status, resp.reason if doc is None else http.client.responses.get(status, '(unknown)')
            except HTTPError:
                if not is_dns_working(timeout=5, check=options.use_dns_check):
                    no_internet.signal()
                    continue
                raise

    @staticmethod
    def _ratelimit_sleep(headers):
        # Daily ratelimit
        if headers.get('X-Ratelimit-Perday-Remaining') == '0':
            reset = headers.get('X-Ratelimit-Perday-Reset')
            try:
                freset = float(reset)  # pytype: disable=wrong-arg-types
            except (TypeError, ValueError):
                logger.error("Expected numerical X-Ratelimit-Perday-Reset, got {!r}\n".format(reset))
                msg = 'sometime tomorrow'
            else:
                treset = datetime.now() + timedelta(seconds=freset)
                msg = 'at {}'.format(treset.ctime())
            raise RuntimeError('{}: Daily API ratelimit exceeded. Resume with --continue after reset {}.\n'.format(
                logger.backup_account, msg
            ))

        # Hourly ratelimit
        reset = headers.get('X-Ratelimit-Perhour-Reset')
        if reset is None:
            return False

        try:
            sleep_dur = float(reset)
        except ValueError:
            logger.error("Expected numerical X-Ratelimit-Perhour-Reset, got '{}'\n".format(reset), account=True)
            return False

        hours, remainder = divmod(abs(sleep_dur), 3600)
        minutes, seconds = divmod(remainder, 60)
        sleep_dur_str = ' '.join(str(int(t[0])) + t[1] for t in ((hours, 'h'), (minutes, 'm'), (seconds, 's')) if t[0])

        if sleep_dur < 0:
            logger.warn('Warning: X-Ratelimit-Perhour-Reset is {} in the past\n'.format(sleep_dur_str), account=True)
            return True
        if sleep_dur > 3600:
            treset = datetime.now() + timedelta(seconds=sleep_dur)
            raise RuntimeError('{}: Refusing to sleep for {}. Resume with --continue at {}.'.format(
                logger.backup_account, sleep_dur_str, treset.ctime(),
            ))

        logger.warn('Hit hourly ratelimit, sleeping for {} as requested\n'.format(sleep_dur_str), account=True)
        time.sleep(sleep_dur + 1)  # +1 to be sure we're past the reset
        return True


def add_exif(image_name, tags):
    assert pyexiv2 is not None
    try:
        metadata = pyexiv2.ImageMetadata(image_name)
        metadata.read()
    except OSError as e:
        logger.error('Error reading metadata for image {!r}: {!r}\n'.format(image_name, e))
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
    except OSError as e:
        logger.error('Writing metadata failed for tags {} in {!r}: {!r}\n'.format(tags, image_name, e))


def save_style():
    with open_text(backup_css) as css:
        css.write('''\
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


def find_files(path, match=None):
    try:
        it = os.scandir(path)
    except FileNotFoundError:
        return  # ignore nonexistent dir
    with it:
        yield from (e.path for e in it if match is None or match(e.name))


def find_post_files():
    path = path_to(post_dir)
    if not options.dirs:
        yield from find_files(path, lambda n: n.endswith(post_ext))
        return

    indexes = (join(e, dir_index) for e in find_files(path))
    yield from filter(os.path.exists, indexes)


def match_avatar(name):
    return name.startswith(avatar_base + '.')


def get_avatar(prev_archive):
    if prev_archive is not None:
        # Copy old avatar, if present
        avatar_matches = find_files(join(prev_archive, theme_dir), match_avatar)
        src = next(avatar_matches, None)
        if src is not None:
            path_parts = (theme_dir, split(src)[-1])
            cpy_res = maybe_copy_media(prev_archive, path_parts)
            if cpy_res:
                return  # We got the avatar
    if options.no_get:
        return  # Don't download the avatar

    url = 'https://api.tumblr.com/v2/blog/%s/avatar' % blog_name
    avatar_dest = avatar_fpath = open_file(lambda f: f, (theme_dir, avatar_base))

    # Remove old avatars
    avatar_matches = find_files(theme_dir, match_avatar)
    if next(avatar_matches, None) is not None:
        return  # Do not clobber

    def adj_bn(old_bn, f):
        # Give it an extension
        kind = filetype.guess(f)
        if kind:
            return avatar_fpath + '.' + kind.extension
        return avatar_fpath

    # Download the image
    assert wget_retrieve is not None
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
    if options.no_get:
        return  # Don't download the style

    url = 'https://%s/' % blog_name
    try:
        resp = urlopen(url, options)
        page_data = resp.data
    except HTTPError as e:
        logger.error('URL is {}\nError retrieving style: {}\n'.format(url, e))
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

    try:
        os.stat(srcpath)
    except FileNotFoundError:
        return False  # Source does not exist

    try:
        os.stat(dstpath)
    except FileNotFoundError:
        pass  # Destination does not exist yet
    else:
        return True  # Don't overwrite

    with open_outfile('wb', *path_parts) as dstf:
        copyfile(srcpath, dstf.name)
        shutil.copystat(srcpath, dstf.name)

    return True  # Copied


def check_optional_modules():
    if options.exif:
        if pyexiv2 is None:
            raise RuntimeError("--exif: module 'pyexiv2' is not installed")
        if not hasattr(pyexiv2, 'ImageMetadata'):
            raise RuntimeError("--exif: module 'pyexiv2' is missing features, perhaps you need 'py3exiv2'?")
    if options.filter is not None and jq is None:
        raise RuntimeError("--filter: module 'jq' is not installed")
    if options.save_notes or options.copy_notes:
        load_bs4('save notes' if options.save_notes else 'copy notes')
    if options.save_video and not (have_module('yt_dlp') or have_module('youtube_dl')):
        raise RuntimeError("--save-video: module 'youtube_dl' is not installed")



def import_youtube_dl():
    global ytdl_module
    if ytdl_module is not None:
        return ytdl_module

    try:
        import yt_dlp
    except ImportError:
        pass
    else:
        ytdl_module = yt_dlp
        return ytdl_module

    import youtube_dl

    ytdl_module = youtube_dl
    return ytdl_module


class Index:
    index: DefaultDict[int, DefaultDict[int, List['LocalPost']]]

    def __init__(self, blog, body_class='index'):
        self.blog = blog
        self.body_class = body_class
        self.index = defaultdict(lambda: defaultdict(list))

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
            idx.write('<footer><p>Generated on %s by <a href=https://github.com/'
                'bbolli/tumblr-utils>tumblr-utils</a>.</p></footer>\n' % strftime('%x %X')
            )

    def save_year(self, idx, archives, index_dir, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=options.reverse_index):
            tm = time.localtime(time.mktime((year, month, 3, 0, 0, 0, 0, 0, -1)))
            month_name = self.save_month(archives, index_dir, year, month, tm)
            idx.write('    <li><a href={} title="{} post(s)">{}</a></li>\n'.format(
                urlpathjoin(archive_dir, month_name), len(self.index[year][month]), strftime('%B', tm)
            ))
        idx.write('</ul>\n\n')

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
        first_file: Optional[str] = None
        for page, start in enumerate(range(0, posts_month, posts_page), start=1):

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
        super().__init__(blog, 'tag-archive')
        self.name = name


class Indices:
    def __init__(self, blog):
        self.blog = blog
        self.main_index = Index(blog)
        self.tags = {}

    def build_index(self):
        for post in map(LocalPost, find_post_files()):
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
                "Tag ‛%s’" % index.name
            )
            tag_index.append('    <li><a href={}>{}</a></li>'.format(
                urlpathjoin(digest, dir_index), escape(index.name)
            ))
        tag_index.extend(['</ul>', ''])
        with open_text(tag_index_dir, dir_index) as f:
            f.write('\n'.join(tag_index))


class TumblrBackup:
    def __init__(self):
        self.failed_blogs = []
        self.postfail_blogs = []
        self.total_count = 0
        self.post_count = 0
        self.filter_skipped = 0
        self.title: Optional[str] = None
        self.subtitle: Optional[str] = None
        self.pa_options: Optional[JSONDict] = None
        self.media_list_file: Optional[TextIO] = None
        self.mlf_seen: Set[int] = set()
        self.mlf_lock = threading.Lock()

    def exit_code(self):
        if self.failed_blogs or self.postfail_blogs:
            return EXIT_ERRORS
        if self.total_count == 0 and not options.json_info:
            return EXIT_NOPOSTS
        return EXIT_SUCCESS

    def header(self, title='', body_class='', subtitle='', avatar=False):
        root_rel = {
            'index': '', 'tag-index': '..', 'tag-archive': '../..'
        }.get(body_class, save_dir)
        css_rel = urlpathjoin(root_rel, custom_css if have_custom_css else backup_css)
        if body_class:
            body_class = ' class=' + body_class
        h = '''<!DOCTYPE html>

<meta charset=%s>
<title>%s</title>
<link rel=stylesheet href=%s>

<body%s>

<header>
''' % (FILE_ENCODING, self.title, css_rel, body_class)
        if avatar:
            avatar_matches = find_files(path_to(theme_dir), match_avatar)
            avatar_path = next(avatar_matches, None)
            if avatar_path is not None:
                h += '<img src={} alt=Avatar>\n'.format(urlpathjoin(root_rel, theme_dir, split(avatar_path)[1]))
        if title:
            h += '<h1>%s</h1>\n' % title
        if subtitle:
            h += '<p class=subtitle>%s</p>\n' % subtitle
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
    def get_post_timestamp(post, BeautifulSoup_):
        if TYPE_CHECKING:
            from bs4 import BeautifulSoup
        else:
            BeautifulSoup = BeautifulSoup_

        with open(post, encoding=FILE_ENCODING) as pf:
            soup = BeautifulSoup(pf, 'lxml')
        postdate = cast(Tag, soup.find('time'))['datetime']
        # datetime.fromisoformat does not understand 'Z' suffix
        return int(datetime.strptime(cast(str, postdate), '%Y-%m-%dT%H:%M:%SZ').timestamp())

    @classmethod
    def process_existing_backup(cls, account, prev_archive):
        complete_backup = os.path.exists(path_to('.complete'))
        try:
            with open(path_to('.first_run_options'), encoding=FILE_ENCODING) as f:
                first_run_options = json.load(f)
        except FileNotFoundError:
            first_run_options = None

        class Options:
            def __init__(self, fro): self.fro = fro
            def differs(self, opt): return opt not in self.fro or orig_options[opt] != self.fro[opt]
            def first(self, opts): return {opt: self.fro.get(opt, '<not present>') for opt in opts}
            @staticmethod
            def this(opts): return {opt: orig_options[opt] for opt in opts}

        # These options must always match
        backdiff_nondef = None
        if first_run_options is not None:
            opts = Options(first_run_options)
            mustmatchdiff = tuple(filter(opts.differs, MUST_MATCH_OPTIONS))
            if mustmatchdiff:
                raise RuntimeError('{}: The script was given {} but the existing backup was made with {}'.format(
                    account, opts.this(mustmatchdiff), opts.first(mustmatchdiff)))

            backdiff = tuple(filter(opts.differs, BACKUP_CHANGING_OPTIONS))
            if complete_backup:
                # Complete archives may be added to with different options
                if (
                    options.resume
                    and first_run_options.get('count') is None
                    and (orig_options['period'] or [0, 0])[0] >= (first_run_options.get('period') or [0, 0])[0]
                ):
                    raise RuntimeError('{}: Cannot continue complete backup that was not stopped early with --count or '
                                       '--period'.format(account))
            elif options.resume:
                backdiff_nondef = tuple(opt for opt in backdiff if orig_options[opt] != parser.get_default(opt))
                if backdiff_nondef and not options.ignore_diffopt:
                    raise RuntimeError('{}: The script was given {} but the existing backup was made with {}. You may '
                                       'skip this check with --ignore-diffopt.'.format(
                                            account, opts.this(backdiff_nondef), opts.first(backdiff_nondef)))
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
                with open(join(prev_archive, '.first_run_options'), encoding=FILE_ENCODING) as f:
                    pa_options = json.load(f)
            except FileNotFoundError:
                pa_options = None

            # These options must always match
            if pa_options is not None:
                pa_opts = Options(pa_options)
                mustmatchdiff = tuple(filter(pa_opts.differs, PREV_MUST_MATCH_OPTIONS))
                if mustmatchdiff:
                    raise RuntimeError('{}: The script was given {} but the previous archive was made with {}'.format(
                        account, pa_opts.this(mustmatchdiff), pa_opts.first(mustmatchdiff)))

        oldest_tstamp = None
        if options.resume or not complete_backup:
            # Read every post to find the oldest timestamp already saved
            post_glob = list(find_post_files())
            if not options.resume:
                pass  # No timestamp needed but may want to know if posts are present
            elif not post_glob:
                raise RuntimeError('{}: Cannot continue empty backup'.format(account))
            else:
                logger.warn('Found incomplete backup.\n', account=True)
                BeautifulSoup = load_bs4('continue incomplete backup')
                if options.likes:
                    logger.warn('Finding oldest liked post (may take a while)\n', account=True)
                    oldest_tstamp = min(cls.get_post_timestamp(post, BeautifulSoup) for post in post_glob)
                else:
                    post_min = min(post_glob, key=lambda f: int(splitext(split(f)[1])[0]))
                    oldest_tstamp = cls.get_post_timestamp(post_min, BeautifulSoup)
                logger.info(
                    'Backing up posts before timestamp={} ({})\n'.format(oldest_tstamp, time.ctime(oldest_tstamp)),
                    account=True,
                )

        write_fro = False
        if backdiff_nondef is not None:
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
            logger.warn('Warning: Unknown media path options for previous archive, assuming they match ours\n',
                        account=True)
            pa_options = {opt: getattr(options, opt) for opt in MEDIA_PATH_OPTIONS}

        return oldest_tstamp, pa_options, write_fro

    def record_media(self, ident: int, urls: Set[str]) -> None:
        with self.mlf_lock:
            if self.media_list_file is not None and ident not in self.mlf_seen:
                json.dump(dict(post=ident, media=sorted(urls)), self.media_list_file, separators=(',', ':'))
                self.media_list_file.write('\n')
                self.mlf_seen.add(ident)

    def backup(self, account, prev_archive):
        """makes single files and an index for every post on a public Tumblr blog account"""

        base = get_api_url(account)

        # make sure there are folders to save in
        global save_folder, media_folder, post_ext, post_dir, save_dir, have_custom_css
        if options.json_info:
            pass  # Not going to save anything
        elif options.blosxom:
            save_folder = root_folder
            post_ext = '.txt'
            post_dir = os.curdir
            post_class: Type[TumblrPost] = BlosxomPost
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

        if options.idents:
            # Normalize idents
            options.idents.sort(reverse=True)

        if options.incremental or options.resume:
            post_glob = list(find_post_files())

        ident_max = None
        if options.incremental and post_glob:
            if options.likes:
                # Read every post to find the newest timestamp already saved
                logger.warn('Finding newest liked post (may take a while)\n', account=True)
                BeautifulSoup = load_bs4('backup likes incrementally')
                ident_max = max(self.get_post_timestamp(post, BeautifulSoup) for post in post_glob)
                logger.info('Backing up posts after timestamp={} ({})\n'.format(ident_max, time.ctime(ident_max)),
                            account=True)
            else:
                # Get the highest post id already saved
                ident_max = max(int(splitext(split(f)[1])[0]) for f in post_glob)
                logger.info('Backing up posts after id={}\n'.format(ident_max), account=True)

        if options.resume:
            # Update skip and count based on where we left off
            options.skip = 0
            self.post_count = len(post_glob)

        logger.status('Getting basic information\r')

        api_parser = ApiParser(base, account)
        if not api_parser.read_archive(prev_archive):
            self.failed_blogs.append(account)
            return
        resp = api_parser.get_initial()
        if not resp:
            self.failed_blogs.append(account)
            return

        # collect all the meta information
        if options.likes:
            if not resp.get('blog', {}).get('share_likes', True):
                logger.error('{} does not have public likes\n'.format(account))
                self.failed_blogs.append(account)
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

        if options.json_info:
            posts = resp[posts_key]
            info = {'uuid': blog.get('uuid'),
                    'post_count': count_estimate,
                    'last_post_ts': posts[0]['timestamp'] if posts else None}
            json.dump(info, sys.stdout)
            return

        if write_fro:
            # Blog directory gets created here
            with open_text('.first_run_options') as f:
                f.write(json.dumps(orig_options))

        def build_index():
            logger.status('Getting avatar and style\r')
            get_avatar(prev_archive)
            get_style(prev_archive)
            if not have_custom_css:
                save_style()
            logger.status('Building index\r')
            ix = Indices(self)
            ix.build_index()
            ix.save_index()

            if not (account in self.failed_blogs or os.path.exists(path_to('.complete'))):
                # Make .complete file
                sf: Optional[int]
                if os.name == 'posix':  # Opening directories and fdatasync are POSIX features
                    sf = opendir(save_folder, os.O_RDONLY)
                else:
                    sf = None
                try:
                    if sf is not None:
                        fdatasync(sf)
                    with open(open_file(lambda f: f, ('.complete',)), 'wb') as f:
                        fsync(f)
                    if sf is not None:
                        fdatasync(sf)
                finally:
                    if sf is not None:
                        os.close(sf)

        if not options.blosxom and options.count == 0:
            build_index()
            return

        # use the meta information to create a HTML header
        TumblrPost.post_header = self.header(body_class='post')

        jq_filter = request_sets = None
        if options.filter is not None:
            assert jq is not None
            jq_filter = jq.compile(options.filter)
        if options.request is not None:
            request_sets = {typ: set(tags) for typ, tags in options.request.items()}

        # start the thread pool
        backup_pool = ThreadPool()

        before = options.period[1] if options.period else None
        if oldest_tstamp is not None:
            before = oldest_tstamp if before is None else min(before, oldest_tstamp)
        if before is not None and api_parser.dashboard_only_blog:
            logger.warn('Warning: skipping posts on a dashboard-only blog is slow\n', account=True)

        # returns whether any posts from this batch were saved
        def _backup(posts):
            def sort_key(x): return x['liked_timestamp'] if options.likes else int(x['id'])
            oldest_date = None
            for p in sorted(posts, key=sort_key, reverse=True):
                no_internet.check()
                enospc.check()
                post = post_class(p, account, prev_archive, self.pa_options, self.record_media)
                oldest_date = post.date
                if before is not None and post.date >= before:
                    if api_parser.dashboard_only_blog:
                        continue  # cannot request 'before' with the svc API
                    raise RuntimeError('Found post with date ({}) newer than before param ({})'.format(
                        post.date, before))
                if ident_max is None:
                    pass  # No limit
                elif (p['liked_timestamp'] if options.likes else int(post.ident)) <= ident_max:
                    logger.info('Stopping backup: Incremental backup complete\n', account=True)
                    return False, oldest_date
                if options.period and post.date < options.period[0]:
                    logger.info('Stopping backup: Reached end of period\n', account=True)
                    return False, oldest_date
                if next_ident is not None and int(post.ident) != next_ident:
                    logger.error("post '{}' not found\n".format(next_ident), account=True)
                    return False, oldest_date
                if request_sets:
                    if post.typ not in request_sets:
                        continue
                    tags = request_sets[post.typ]
                    if not (TAG_ANY in tags or tags & {t.lower() for t in post.tags}):
                        continue
                if options.no_reblog and post_is_reblog(p):
                    continue
                if options.only_reblog and not post_is_reblog(p):
                    continue
                if jq_filter:
                    try:
                        matches = jq_filter.input(p).first()
                    except StopIteration:
                        matches = False
                    if not matches:
                        self.filter_skipped += 1
                        continue
                if os.path.exists(path_to(*post.get_path())) and options.no_post_clobber:
                    continue  # Post exists and no-clobber enabled

                with multicond:
                    while backup_pool.queue.qsize() >= backup_pool.queue.maxsize:
                        no_internet.check(release=True)
                        enospc.check(release=True)
                        # All conditions false, wait for a change
                        multicond.wait((backup_pool.queue.not_full, no_internet.cond, enospc.cond))
                    backup_pool.add_work(post.save_post)

                self.post_count += 1
                if options.count and self.post_count >= options.count:
                    logger.info('Stopping backup: Reached limit of {} posts\n'.format(options.count), account=True)
                    return False, oldest_date
            return True, oldest_date

        api_thread = AsyncCallable(main_thread_lock, api_parser.apiparse, 'API Thread')

        next_ident: Optional[int] = None
        if options.idents is not None:
            remaining_idents = options.idents.copy()
            count_estimate = len(remaining_idents)

        mlf: Optional[ContextManager[TextIO]]
        if options.media_list:
            mlf = open_text('media.json', mode='r+')
            self.media_list_file = mlf.__enter__()
            self.mlf_seen.clear()
            for line in self.media_list_file:
                doc = json.loads(line)
                self.mlf_seen.add(doc['post'])
        else:
            mlf = None

        try:
            # Get the JSON entries from the API, which we can only do for MAX_POSTS posts at once.
            # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
            i = options.skip

            while True:
                # find the upper bound
                logger.status('Getting {}posts {} to {}{}\r'.format(
                    'liked ' if options.likes else '', i, i + MAX_POSTS - 1,
                    '' if count_estimate is None else ' (of {} expected)'.format(count_estimate),
                ))

                if options.idents is not None:
                    try:
                        next_ident = remaining_idents.pop(0)
                    except IndexError:
                        # if the last requested post does not get backed up we end up here
                        logger.info('Stopping backup: End of requested posts\n', account=True)
                        break

                with multicond:
                    api_thread.put(MAX_POSTS, i, before, next_ident)

                    while not api_thread.response.qsize():
                        no_internet.check(release=True)
                        enospc.check(release=True)
                        # All conditions false, wait for a change
                        multicond.wait((api_thread.response.not_empty, no_internet.cond, enospc.cond))

                    resp = api_thread.get(block=False)

                if resp is None:
                    self.failed_blogs.append(account)
                    break

                posts = resp[posts_key]
                if not posts:
                    logger.info('Backup complete: Found empty set of posts\n', account=True)
                    break

                res, oldest_date = _backup(posts)
                if not res:
                    break

                if options.likes and prev_archive is None:
                    next_ = resp['_links'].get('next')
                    if next_ is None:
                        logger.info('Backup complete: Found end of likes\n', account=True)
                        break
                    before = int(next_['query_params']['before'])
                elif before is not None and not api_parser.dashboard_only_blog:
                    assert oldest_date <= before
                    if oldest_date == before:
                        oldest_date -= 1
                    before = oldest_date

                if options.idents is None:
                    i += MAX_POSTS
                else:
                    i += 1

            api_thread.quit()
            backup_pool.wait()  # wait until all posts have been saved
        except:
            api_thread.quit()
            backup_pool.cancel()  # ensure proper thread pool termination
            raise
        finally:
            if mlf is not None:
                mlf.__exit__(*sys.exc_info())
                self.media_list_file = None

        if backup_pool.errors:
            self.postfail_blogs.append(account)

        # postprocessing
        if not options.blosxom and self.post_count:
            build_index()

        logger.status(None)
        skipped_msg = (', {} did not match filter'.format(self.filter_skipped)) if self.filter_skipped else ''
        logger.warn(
            '{} {}posts backed up{}\n'.format(self.post_count, 'liked ' if options.likes else '', skipped_msg),
            account=True,
        )
        self.total_count += self.post_count


class TumblrPost:
    post_header = ''  # set by TumblrBackup.backup()

    def __init__(
        self,
        post: JSONDict,
        backup_account: str,
        prev_archive: Optional[str],
        pa_options: Optional[JSONDict],
        record_media: Callable[[int, Set[str]], None],
    ) -> None:
        self.post = post
        self.backup_account = backup_account
        self.prev_archive = prev_archive
        self.pa_options = pa_options
        self.record_media = record_media
        self.post_media: Set[str] = set()
        self.creator = post.get('blog_name') or post['tumblelog']
        self.ident = str(post['id'])
        self.url = post['post_url']
        self.shorturl = post['short_url']
        self.typ = str(post['type'])
        self.date: float = post['liked_timestamp' if options.likes else 'timestamp']
        self.isodate = datetime.utcfromtimestamp(self.date).isoformat() + 'Z'
        self.tm = time.localtime(self.date)
        self.title = ''
        self.tags: str = post['tags']
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
        self.post_media.clear()

        def append(s, fmt='%s'):
            content.append(fmt % s)

        def get_try(elt) -> Union[Any, Literal['']]:
            return post.get(elt, '')

        def append_try(elt, fmt='%s'):
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
                append(escape(src), '<img alt="" src="%s">')
                if url:
                    content[-1] = '<a href="%s">%s</a>' % (escape(url), content[-1])
                content[-1] = '<p>' + content[-1] + '</p>'
                if p['caption']:
                    append(p['caption'], '<p>%s</p>')
            append_try('caption')

        elif self.typ == 'link':
            url = post['url']
            self.title = '<a href="%s">%s</a>' % (escape(url), post['title'] or url)
            append_try('description')

        elif self.typ == 'quote':
            append(post['text'], '<blockquote><p>%s</p></blockquote>')
            append_try('source', '<p>%s</p>')

        elif self.typ == 'video':
            src = ''
            if (options.save_video or options.save_video_tumblr) \
                    and post['video_type'] == 'tumblr':
                src = self.get_media_url(post['video_url'], '.mp4')
            elif options.save_video:
                src = self.get_youtube_url(self.url)
                if not src:
                    logger.warn('Unable to download video in post #{}\n'.format(self.ident))
            if src:
                append('<p><video controls><source src="%s" type=video/mp4>%s<br>\n<a href="%s">%s</a></video></p>' % (
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
                append('<p><audio controls><source src="{src}" type=audio/mpeg>{}<br>\n<a href="{src}">{}'
                       '</a></audio></p>'
                       .format('Your browser does not support the audio element.', 'Audio file', src=src_))

            src = None
            audio_url = get_try('audio_url') or get_try('audio_source_url')
            if options.save_audio:
                if post['audio_type'] == 'tumblr':
                    if audio_url.startswith('https://a.tumblr.com/'):
                        src = self.get_media_url(audio_url, '.mp3')
                    elif audio_url.startswith('https://www.tumblr.com/audio_file/'):
                        audio_url = 'https://a.tumblr.com/{}o1.mp3'.format(urlbasename(urlparse(audio_url).path))
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
                '<br>\n'.join('%(label)s %(phrase)s' % d for d in post['dialogue']),
                '<p>%s</p>'
            )

        else:
            logger.warn("Unknown post type '{}' in post #{}\n".format(self.typ, self.ident))
            append(escape(self.get_json_content()), '<pre>%s</pre>')

        # Write URLs to media.json
        self.record_media(int(self.ident), self.post_media)

        content_str = '\n'.join(content)

        # fix wrongly nested HTML elements
        for p in ('<p>(<({})>)', '(</({})>)</p>'):
            content_str = re.sub(p.format('p|ol|iframe[^>]*'), r'\1', content_str)

        return content_str

    def get_youtube_url(self, youtube_url):
        # determine the media file name
        filetmpl = '%(id)s_%(uploader_id)s_%(title)s.%(ext)s'
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

        if TYPE_CHECKING:
            import youtube_dl
        else:
            youtube_dl = import_youtube_dl()

        ydl = youtube_dl.YoutubeDL(ydl_options)
        ydl.add_default_info_extractors()
        try:
            result = ydl.extract_info(youtube_url, download=False)
            media_filename = youtube_dl.utils.sanitize_filename(filetmpl % result['entries'][0], restricted=True)
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
        return '%s%s/%s%s' % (match.group(1), self.media_url,
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
        return ('%s%s/%s%s' % (match.group(1), self.media_url,
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
        return '%s%s%s' % (match.group(1), saved_name, match.group(3))

    def get_filename(self, parsed_url, image_names, offset=''):
        """Determine the image file name depending on image_names"""
        fname = urlbasename(parsed_url.path)
        ext = urlsplitext(fname)[1]
        if parsed_url.query:
            # Insert the query string to avoid ambiguity for certain URLs (e.g. SoundCloud embeds).
            query_sep = '@' if os.name == 'nt' else '?'
            if ext:
                extwdot = '.{}'.format(ext)
                fname = fname[:-len(extwdot)] + query_sep + parsed_url.query + extwdot
            else:
                fname = fname + query_sep + parsed_url.query
        if image_names == 'i':
            return self.ident + offset + ext
        if image_names == 'bi':
            return self.backup_account + '_' + self.ident + offset + ext
        # delete characters not allowed under Windows
        return re.sub(r'[:<>"/\\|*?]', '', fname) if os.name == 'nt' else fname

    def download_media(self, url, filename=None, offset='', extension=None):
        parsed_url = urlparse(url, 'http')
        hostname = parsed_url.hostname
        if parsed_url.scheme not in ('http', 'https') or not hostname:
            return None  # This URL does not follow our basic assumptions

        # Make a sane directory to represent the host
        try:
            hostname = hostname.encode('idna').decode('ascii')
        except UnicodeError:
            hostname = hostname
        if hostname in ('.', '..'):
            hostname = hostname.replace('.', '%2E')
        if parsed_url.port not in (None, (80 if parsed_url.scheme == 'http' else 443)):
            hostname += '{}{}'.format('+' if os.name == 'nt' else ':', parsed_url.port)

        def get_path(media_dir, image_names, hostdirs):
            if filename is not None:
                fname = filename
            else:
                fname = self.get_filename(parsed_url, image_names, offset)
                if extension is not None:
                    fname = splitext(fname)[0] + extension
            parts = (media_dir,) + ((hostname,) if hostdirs else ()) + (fname,)
            return parts

        path_parts = get_path(self.media_dir, options.image_names, options.hostdirs)
        media_path = path_to(*path_parts)

        # prevent racing of existence check and download
        with downloading_media_cond:
            while media_path in downloading_media:
                downloading_media_cond.wait()
            downloading_media.add(media_path)

        try:
            return self._download_media_inner(url, get_path, path_parts, media_path)
        finally:
            with downloading_media_cond:
                downloading_media.remove(media_path)
                downloading_media_cond.notify_all()

    def _download_media_inner(self, url, get_path, path_parts, media_path):
        self.post_media.add(url)

        if self.prev_archive is None:
            cpy_res = False
        else:
            assert self.pa_options is not None
            pa_path_parts = get_path(
                join(post_dir, self.ident) if self.pa_options['dirs'] else media_dir,
                self.pa_options['image_names'], self.pa_options['hostdirs'],
            )
            cpy_res = maybe_copy_media(self.prev_archive, path_parts, pa_path_parts)
        file_exists = os.path.exists(media_path)
        if not (cpy_res or file_exists):
            if options.no_get:
                return None
            # We don't have the media and we want it
            assert wget_retrieve is not None
            dstpath = open_file(lambda f: f, path_parts)
            try:
                wget_retrieve(url, dstpath, post_id=self.ident, post_timestamp=self.post['timestamp'])
            except WGError as e:
                e.log()
                return None
        if file_exists:
            try:
                st = os.stat(media_path)
            except FileNotFoundError:
                pass  # skip
            else:
                if st.st_mtime > self.post['timestamp']:
                    touch(media_path, self.post['timestamp'])

        return path_parts[-1]

    def get_post(self):
        """returns this post in HTML"""
        typ = ('liked-' if options.likes else '') + self.typ
        post = self.post_header + '<article class=%s id=p-%s>\n' % (typ, self.ident)
        post += '<header>\n'
        if options.likes:
            post += '<p><a href=\"https://{0}.tumblr.com/\" class=\"tumblr_blog\">{0}</a>:</p>\n'.format(self.creator)
        post += '<p><time datetime=%s>%s</time>\n' % (self.isodate, strftime('%x %X', self.tm))
        post += '<a class=llink href={}>¶</a>\n'.format(urlpathjoin(save_dir, post_dir, self.llink))
        post += '<a href=%s>●</a>\n' % self.shorturl
        if self.reblogged_from and self.reblogged_from != self.reblogged_root:
            post += '<a href=%s>⬀</a>\n' % self.reblogged_from
        if self.reblogged_root:
            post += '<a href=%s>⬈</a>\n' % self.reblogged_root
        post += '</header>\n'
        content = self.get_content()
        if self.title:
            post += '<h2>%s</h2>\n' % self.title
        post += content
        foot = []
        if self.tags:
            foot.append(''.join(self.tag_link(t) for t in self.tags))
        if self.source_title and self.source_url:
            foot.append('<a title=Source href=%s>%s</a>' %
                (self.source_url, self.source_title)
            )

        notes_html = ''

        if options.save_notes or options.copy_notes:
            if TYPE_CHECKING:
                from bs4 import BeautifulSoup
            else:
                BeautifulSoup = load_bs4('save notes' if options.save_notes else 'copy notes')

        if options.copy_notes:
            # Copy notes from prev_archive (or here)
            prev_archive = save_folder if options.reuse_json else self.prev_archive
            assert prev_archive is not None
            try:
                with open(join(prev_archive, post_dir, self.ident + post_ext)) as post_file:
                    soup = BeautifulSoup(post_file, 'lxml')
            except FileNotFoundError:
                pass  # skip
            else:
                notes = cast(Tag, soup.find('ol', class_='notes'))
                if notes is not None:
                    notes_html = ''.join([n.prettify() for n in notes.find_all('li')])

        if options.save_notes and self.backup_account not in disable_note_scraper and not notes_html.strip():
            from . import note_scraper

            # Scrape and save notes
            while True:
                ns_stdout_rd, ns_stdout_wr = multiprocessing.Pipe(duplex=False)
                ns_msg_queue: SimpleQueue[Tuple[LogLevel, str]] = multiprocessing.SimpleQueue()
                try:
                    args = (ns_stdout_wr, ns_msg_queue, self.url, self.ident,
                            options.no_ssl_verify, options.user_agent, options.cookiefile, options.notes_limit,
                            options.use_dns_check)
                    process = multiprocessing.Process(target=note_scraper.main, args=args)
                    process.start()
                except:
                    ns_stdout_rd.close()
                    ns_msg_queue._reader.close()  # type: ignore[attr-defined]
                    raise
                finally:
                    ns_stdout_wr.close()
                    ns_msg_queue._writer.close()  # type: ignore[attr-defined]

                try:
                    try:
                        while True:
                            level, msg = ns_msg_queue.get()
                            logger.log(level, msg)
                    except EOFError:
                        pass  # Exit loop
                    finally:
                        ns_msg_queue.close()  # type: ignore[attr-defined]

                    with ConnectionFile(ns_stdout_rd) as stdout:
                        notes_html = stdout.read()

                    process.join()
                except:
                    process.terminate()
                    process.join()
                    raise

                if process.exitcode == 2:  # EXIT_SAFE_MODE
                    # Safe mode is blocking us, disable note scraping for this blog
                    notes_html = ''
                    with disablens_lock:
                        # Check if another thread already set this
                        if self.backup_account not in disable_note_scraper:
                            disable_note_scraper.add(self.backup_account)
                            logger.info('[Note Scraper] Blocked by safe mode - scraping disabled for {}\n'.format(
                                self.backup_account
                            ))
                elif process.exitcode == 3:  # EXIT_NO_INTERNET
                    no_internet.signal()
                    continue
                break

        notes_str = '{} note{}'.format(self.note_count, 's'[self.note_count == 1:])
        if notes_html.strip():
            foot.append('<details><summary>{}</summary>\n'.format(notes_str))
            foot.append('<ol class="notes">')
            foot.append(notes_html)
            foot.append('</ol></details>')
        else:
            foot.append(notes_str)

        if foot:
            post += '\n<footer>{}</footer>'.format('\n'.join(foot))
        post += '\n</article>\n'
        return post

    @staticmethod
    def tag_link(tag):
        tag_disp = escape(TAG_FMT.format(tag))
        if not TAGLINK_FMT:
            return tag_disp + ' '
        url = TAGLINK_FMT.format(domain=blog_name, tag=quote(to_bytes(tag)))
        return '<a href=%s>%s</a>\n' % (url, tag_disp)

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
            logger.error('Caught exception while saving post {}:\n{}'.format(self.ident, traceback.format_exc()))
            return False
        return True

    def get_json_content(self):
        return json.dumps(self.post, sort_keys=True, indent=4, separators=(',', ': '))

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


class LocalPost:
    def __init__(self, post_file):
        self.post_file = post_file
        if options.tag_index:
            with open(post_file, encoding=FILE_ENCODING) as f:
                post = f.read()
            # extract all URL-encoded tags
            self.tags: List[Tuple[str, str]] = []
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
        self.date: float = os.stat(post_file).st_mtime
        self.tm = time.localtime(self.date)

    def get_post(self, in_tag_index):
        with open(self.post_file, encoding=FILE_ENCODING) as f:
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


class ThreadPool:
    queue: LockedQueue[Callable[[], None]]

    def __init__(self, max_queue=1000):
        self.queue = LockedQueue(main_thread_lock, max_queue)
        self.quit = threading.Condition(main_thread_lock)
        self.quit_flag = False
        self.abort_flag = False
        self.errors = False
        self.threads = [threading.Thread(target=self.handler) for _ in range(options.threads)]
        for t in self.threads:
            t.start()

    def add_work(self, *args, **kwargs):
        self.queue.put(*args, **kwargs)

    def wait(self):
        with multicond:
            self._print_remaining(self.queue.qsize())
            self.quit_flag = True
            self.quit.notify_all()
            while self.queue.unfinished_tasks:
                no_internet.check(release=True)
                enospc.check(release=True)
                # All conditions false, wait for a change
                multicond.wait((self.queue.all_tasks_done, no_internet.cond, enospc.cond))

    def cancel(self):
        with main_thread_lock:
            self.abort_flag = True
            self.quit.notify_all()
            no_internet.destroy()
            enospc.destroy()

        for i, t in enumerate(self.threads, start=1):
            logger.status('Stopping threads {}{}\r'.format(' ' * i, '.' * (len(self.threads) - i)))
            t.join()

        logger.info('Backup canceled.\n')

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
                    self._print_remaining(qsize)

            try:
                while True:
                    try:
                        success = work()
                        break
                    except OSError as e:
                        if e.errno == errno.ENOSPC:
                            enospc.signal()
                            continue
                        raise
            finally:
                self.queue.task_done()
            if not success:
                self.errors = True

    @staticmethod
    def _print_remaining(qsize):
        if qsize:
            logger.status('{} remaining posts to save\r'.format(qsize))
        else:
            logger.status('Waiting for worker threads to finish\r')


def main():
    global parser, options, orig_options, API_KEY, wget_retrieve

    # The default of 'fork' can cause deadlocks, even on Linux
    # See https://bugs.python.org/issue40399
    if 'forkserver' in multiprocessing.get_all_start_methods():
        multiprocessing.set_start_method('forkserver')  # Fastest safe option, if supported
    else:
        multiprocessing.set_start_method('spawn')  # Slow but safe

    # Raises SystemExit to terminate gracefully
    def handle_term_signal(signum, frame):
        if sys.is_finalizing():
            return  # Not a good time to exit
        sys.exit(1)
    signal.signal(signal.SIGTERM, handle_term_signal)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, handle_term_signal)


    config_dir = platformdirs.user_config_dir('tumblr-backup', roaming=True, ensure_exists=True)
    config_file = Path(config_dir) / 'config.json'

    if '--set-api-key' in sys.argv[1:]:
        # special argument parsing
        opt, *args = sys.argv[1:]
        if opt != '--set-api-key' or len(args) != 1:
            print(f'{Path(sys.argv[0]).name}: invalid usage', file=sys.stderr)
            return 1
        api_key, = args
        with open(config_file, 'r+') as f:
            cfg = json.load(f)
            cfg['oauth_consumer_key'] = api_key
            f.seek(0)
            json.dump(cfg, f, indent=4)
        return 0


    no_internet.setup(main_thread_lock)
    enospc.setup(main_thread_lock)

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
            super().__call__(
                parser, namespace, TYPE_ANY + ':' + values.replace(',', ':'), option_string,
            )

    class PeriodCallback(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            try:
                pformat = {'y': '%Y', 'm': '%Y%m', 'd': '%Y%m%d'}[values]
            except KeyError:
                periods = values.replace('-', '').split(',')
                if not all(re.match(r'\d{4}(\d\d)?(\d\d)?Z?$', p) for p in periods):
                    parser.error("Period must be 'y', 'm', 'd' or YYYY[MM[DD]][Z]")
                if not (1 <= len(periods) < 3):
                    parser.error('Period must have either one year/month/day or a start and end')
                prange = parse_period_date(periods.pop(0))
                if periods:
                    prange[1] = parse_period_date(periods.pop(0))[0]
            else:
                period = time.strftime(pformat)
                prange = parse_period_date(period)
            setattr(namespace, self.dest, prange)

    class IdFileCallback(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            with open(values) as f:
                setattr(namespace, self.dest, sorted(
                    map(int, (line for line in map(lambda l: l.rstrip('\n'), f) if line)),
                    reverse=True,
                ))

    parser = argparse.ArgumentParser(usage='%(prog)s [options] blog-name ...',
                                     description='Makes a local backup of Tumblr blogs.')
    postexist_group = parser.add_mutually_exclusive_group()
    reblog_group = parser.add_mutually_exclusive_group()
    parser.add_argument('-O', '--outdir', help='set the output directory (default: blog-name)')
    parser.add_argument('-D', '--dirs', action='store_true', help='save each post in its own folder')
    parser.add_argument('-q', '--quiet', action='store_true', help='suppress progress messages')
    postexist_group.add_argument('-i', '--incremental', action='store_true', help='incremental backup mode')
    parser.add_argument('-l', '--likes', action='store_true', help="save a blog's likes, not its posts")
    parser.add_argument('-k', '--skip-images', action='store_false', dest='save_images',
                        help='do not save images; link to Tumblr instead')
    parser.add_argument('--save-video', action='store_true', help='save all video files')
    parser.add_argument('--save-video-tumblr', action='store_true', help='save only Tumblr video files')
    parser.add_argument('--save-audio', action='store_true', help='save audio files')
    parser.add_argument('--save-notes', action='store_true', help='save a list of notes for each post')
    parser.add_argument('--copy-notes', action='store_true', default=None,
                        help='copy the notes list from a previous archive (inverse: --no-copy-notes)')
    parser.add_argument('--no-copy-notes', action='store_false', default=None, dest='copy_notes',
                        help=argparse.SUPPRESS)
    parser.add_argument('--notes-limit', type=int, metavar='COUNT', help='limit requested notes to COUNT, per-post')
    parser.add_argument('--cookiefile', help='cookie file for youtube-dl, --save-notes, and svc API')
    parser.add_argument('-j', '--json', action='store_true', help='save the original JSON source')
    parser.add_argument('-b', '--blosxom', action='store_true', help='save the posts in blosxom format')
    parser.add_argument('-r', '--reverse-month', action='store_false',
                        help='reverse the post order in the monthly archives')
    parser.add_argument('-R', '--reverse-index', action='store_false', help='reverse the index file order')
    parser.add_argument('--tag-index', action='store_true', help='also create an archive per tag')
    postexist_group.add_argument('-a', '--auto', type=int, metavar='HOUR',
                                 help='do a full backup at HOUR hours, otherwise do an incremental backup'
                                      ' (useful for cron jobs)')
    parser.add_argument('-n', '--count', type=int, help='save only COUNT posts')
    parser.add_argument('-s', '--skip', type=int, default=0, help='skip the first SKIP posts')
    parser.add_argument('-p', '--period', action=PeriodCallback,
                        help="limit the backup to PERIOD ('y', 'm', 'd', YYYY[MM[DD]][Z], or START,END)")
    parser.add_argument('-N', '--posts-per-page', type=int, default=50, metavar='COUNT',
                        help='set the number of posts per monthly page, 0 for unlimited')
    parser.add_argument('-Q', '--request', action=RequestCallback,
                        help='save posts matching the request TYPE:TAG:TAG:…,TYPE:TAG:…,…. '
                             'TYPE can be {} or {any}; TAGs can be omitted or a colon-separated list. '
                             'Example: -Q {any}:personal,quote,photo:me:self'
                             .format(', '.join(POST_TYPES), any=TYPE_ANY))
    parser.add_argument('-t', '--tags', action=TagsCallback, dest='request',
                        help='save only posts tagged TAGS (comma-separated values; case-insensitive)')
    parser.add_argument('-T', '--type', action=RequestCallback, dest='request',
                        help='save only posts of type TYPE (comma-separated values from {})'
                             .format(', '.join(POST_TYPES)))
    parser.add_argument('-F', '--filter', help='save posts matching a jq filter (needs jq module)')
    reblog_group.add_argument('--no-reblog', action='store_true', help="don't save reblogged posts")
    reblog_group.add_argument('--only-reblog', action='store_true', help='save only reblogged posts')
    parser.add_argument('-I', '--image-names', choices=('o', 'i', 'bi'), default='o', metavar='FMT',
                        help="image filename format ('o'=original, 'i'=<post-id>, 'bi'=<blog-name>_<post-id>)")
    parser.add_argument('-e', '--exif', action=CSVCallback, default=[], metavar='KW',
                        help='add EXIF keyword tags to each picture'
                             " (comma-separated values; '-' to remove all tags, '' to add no extra tags)")
    parser.add_argument('-S', '--no-ssl-verify', action='store_true', help='ignore SSL verification errors')
    parser.add_argument('--prev-archives', action=CSVCallback, default=[], metavar='DIRS',
                        help='comma-separated list of directories (one per blog) containing previous blog archives')
    parser.add_argument('--no-post-clobber', action='store_true', help='Do not re-download existing posts')
    parser.add_argument('--no-server-timestamps', action='store_false', dest='use_server_timestamps',
                        help="don't set local timestamps from HTTP headers")
    parser.add_argument('--hostdirs', action='store_true', help='Generate host-prefixed directories for media')
    parser.add_argument('--user-agent', help='User agent string to use with HTTP requests')
    parser.add_argument('--skip-dns-check', action='store_false', dest='use_dns_check',
                        help='Skip DNS checks for internet access')
    parser.add_argument('--threads', type=int, default=20, help='number of threads to use for post retrieval')
    postexist_group.add_argument('--continue', action='store_true', dest='resume',
                                 help='Continue an incomplete first backup')
    parser.add_argument('--ignore-diffopt', action='store_true',
                        help='Force backup over an incomplete archive with different options')
    parser.add_argument('--no-get', action='store_true', help="Don't retrieve files not found in --prev-archives")
    postexist_group.add_argument('--reuse-json', action='store_true',
                                 help='Reuse the API responses saved with --json (implies --copy-notes)')
    parser.add_argument('--internet-archive', action='store_true',
                        help='Fall back to the Internet Archive for Tumblr media 403 and 404 responses')
    parser.add_argument('--media-list', action='store_true', help='Save post media URLs to media.json')
    parser.add_argument('--id-file', action=IdFileCallback, dest='idents', metavar='FILE',
                        help='file containing a list of post IDs to save, one per line')
    parser.add_argument('--json-info', action='store_true',
                        help="Just print some info for each blog, don't make a backup")
    parser.add_argument('blogs', nargs='*')
    options = parser.parse_args()
    blogs = options.blogs

    if not blogs:
        parser.error('Missing blog-name')
    if options.auto is not None and options.auto != time.localtime().tm_hour:
        options.incremental = True
    if options.resume or options.incremental:
        # Do not clobber or count posts that were already backed up
        options.no_post_clobber = True
    if options.json_info:
        options.quiet = True
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
    if options.threads < 1:
        parser.error('--threads: must use at least one thread')
    if options.no_get and not (options.prev_archives or options.reuse_json):
        parser.error('--no-get makes no sense without --prev-archives or --reuse-json')
    if options.no_get and options.save_notes:
        logger.warn('Warning: --save-notes uses HTTP regardless of --no-get\n')
    if options.copy_notes and not (options.prev_archives or options.reuse_json):
        parser.error('--copy-notes requires --prev-archives or --reuse-json')
    if options.idents is not None and options.likes:
        parser.error('--id-file not implemented for likes')
    if options.copy_notes is None:
        # Default to True if we may regenerate posts
        options.copy_notes = options.reuse_json and not (options.no_post_clobber or options.mtime_fix)

    # NB: this is done after setting implied options
    orig_options = vars(options).copy()

    check_optional_modules()

    try:
        with open(config_file) as f:
            API_KEY = json.load(f)['oauth_consumer_key']
    except (FileNotFoundError, KeyError):
        print(f"""\
API key not set. To use tumblr-backup:
1. Go to https://www.tumblr.com/oauth/apps and create an app if you don't have one already.
2. Copy the "OAuth Consumer Key" from the app you created.
3. Run `{Path(sys.argv[0]).name} --set-api-key API_KEY`, where API_KEY is the key that you just copied.""",
            file=sys.stderr,
        )
        return 1

    wget_retrieve = WgetRetrieveWrapper(options, logger.log)
    setup_wget(not options.no_ssl_verify, options.user_agent)

    ApiParser.setup()
    tb = TumblrBackup()
    try:
        for i, account in enumerate(blogs):
            logger.backup_account = account
            tb.backup(account, options.prev_archives[i] if options.prev_archives else None)
    except KeyboardInterrupt:
        return EXIT_INTERRUPT

    if tb.failed_blogs:
        logger.warn('Failed to back up {}\n'.format(', '.join(tb.failed_blogs)))
    if tb.postfail_blogs:
        logger.warn('One or more posts failed to save for {}\n'.format(', '.join(tb.postfail_blogs)))
    return tb.exit_code()
