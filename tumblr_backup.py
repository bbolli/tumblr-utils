#!/usr/bin/env python
# encoding: utf-8

# standard Python library imports
from __future__ import with_statement
import codecs
from collections import defaultdict
from datetime import datetime
import errno
from glob import glob
import hashlib
from httplib import HTTPException
import imghdr
try:
    import json
except ImportError:
    import simplejson as json
import locale
import os
from os.path import join, split, splitext
import Queue
import re
import ssl
import sys
import threading
import time
import urllib
import urllib2
import urlparse
from xml.sax.saxutils import escape

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

# Format of displayed tags
TAG_FMT = '#%s'

# Format of tag link URLs; set to None to suppress the links.
# Named placeholders that will be replaced: domain, tag
TAGLINK_FMT = 'http://%(domain)s/tagged/%(tag)s'

# exit codes
EXIT_SUCCESS    = 0
EXIT_NOPOSTS    = 1
# EXIT_OPTPARSE = 2 -- returned by module optparse
EXIT_INTERRUPT  = 3
EXIT_ERRORS     = 4

# add another JPEG recognizer
# see http://www.garykessler.net/library/file_sigs.html
def test_jpg(h, f):
    if h[:3] == '\xFF\xD8\xFF' and h[3] in "\xDB\xE0\xE1\xE2\xE3":
        return 'jpg'

imghdr.tests.append(test_jpg)

# variable directory names, will be set in TumblrBackup.backup()
save_folder = ''
media_folder = ''

# constant names
root_folder = os.getcwdu()
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

POST_TYPES = (
    'text', 'quote', 'link', 'answer', 'video', 'audio', 'photo', 'chat'
)
POST_TYPES_SET = frozenset(POST_TYPES)
TYPE_ANY = 'any'
TAG_ANY = '__all__'

MAX_POSTS = 50

HTTP_TIMEOUT = 90
HTTP_CHUNK_SIZE = 1024 * 1024

# bb-tumblr-backup API key
API_KEY = '8YUsKJvcJxo2MDwmWMDiXZGuMuIbeCwuQGP5ZHSEA4jBJPMnJT'

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass
encoding = 'utf-8'
time_encoding = locale.getlocale(locale.LC_TIME)[1] or encoding


have_ssl_ctx = sys.version_info >= (2, 7, 9)
if have_ssl_ctx:
    ssl_ctx = ssl.create_default_context()
    def urlopen(url):
        return urllib2.urlopen(url, timeout=HTTP_TIMEOUT, context=ssl_ctx)
else:
    def urlopen(url):
        return urllib2.urlopen(url, timeout=HTTP_TIMEOUT)


def log(account, s):
    if not options.quiet:
        if account:
            sys.stdout.write('%s: ' % account)
        sys.stdout.write(s[:-1] + ' ' * 20 + s[-1:])
        sys.stdout.flush()


def mkdir(dir, recursive=False):
    if not os.path.exists(dir):
        try:
            if recursive:
                os.makedirs(dir)
            else:
                os.mkdir(dir)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise


def path_to(*parts):
    return join(save_folder, *parts)


def open_file(open_fn, parts):
    if len(parts) > 1:
        mkdir(path_to(*parts[:-1]), (len(parts) > 2))
    return open_fn(path_to(*parts))


def open_text(*parts):
    return open_file(
        lambda f: codecs.open(f, 'w', encoding, 'xmlcharrefreplace'), parts
    )


def open_media(*parts):
    return open_file(lambda f: open(f, 'wb'), parts)


def strftime(format, t=None):
    if t is None:
        t = time.localtime()
    return time.strftime(format, t).decode(time_encoding)


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
    options.p_start = time.mktime(tm)
    tm[i] += 1
    options.p_stop = time.mktime(tm)


def apiparse(base, count, start=0):
    params = {'api_key': API_KEY, 'limit': count, 'reblog_info': 'true'}
    if start > 0:
        params['offset'] = start
    url = base + '?' + urllib.urlencode(params)
    for _ in range(10):
        try:
            resp = urlopen(url)
            data = resp.read()
        except (EnvironmentError, HTTPException) as e:
            sys.stderr.write("%s getting %s\n" % (e, url))
            continue
        if resp.info().gettype() == 'application/json':
            break
        sys.stderr.write("Unexpected Content-Type: '%s'\n" % resp.info().gettype())
        return None
    else:
        return None
    try:
        doc = json.loads(data)
    except ValueError as e:
        sys.stderr.write('%s: %s\n%d %s %s\n%r\n' % (
            e.__class__.__name__, e, resp.getcode(), resp.msg, resp.info().gettype(), data
        ))
        return None
    return doc if doc.get('meta', {}).get('status', 0) == 200 else None


def add_exif(image_name, tags):
    try:
        metadata = pyexiv2.ImageMetadata(image_name)
        metadata.read()
    except EnvironmentError:
        sys.stderr.write("Error reading metadata for image %s\n" % image_name)
        return
    KW_KEY = 'Iptc.Application2.Keywords'
    if '-' in options.exif:     # remove all tags
        if KW_KEY in metadata.iptc_keys:
            del metadata[KW_KEY]
    else:                       # add tags
        if KW_KEY in metadata.iptc_keys:
            tags |= set(metadata[KW_KEY].value)
        tags = list(tag.strip().lower() for tag in tags | options.exif if tag)
        metadata[KW_KEY] = pyexiv2.IptcTag(KW_KEY, tags)
    try:
        metadata.write()
    except EnvironmentError:
        sys.stderr.write("Writing metadata failed for tags: %s in: %s\n" % (tags, image_name))


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


def get_avatar():
    try:
        resp = urlopen('http://api.tumblr.com/v2/blog/%s/avatar' % blog_name)
        avatar_data = resp.read()
    except (EnvironmentError, HTTPException):
        return
    avatar_file = avatar_base + '.' + imghdr.what(None, avatar_data[:32])
    with open_media(theme_dir, avatar_file) as f:
        f.write(avatar_data)


def get_style():
    """Get the blog's CSS by brute-forcing it from the home page.
    The v2 API has no method for getting the style directly.
    See https://groups.google.com/d/msg/tumblr-api/f-rRH6gOb6w/sAXZIeYx5AUJ"""
    try:
        resp = urlopen('http://%s/' % blog_name)
        page_data = resp.read()
    except (EnvironmentError, HTTPException):
        return
    for match in re.findall(r'(?s)<style type=.text/css.>(.*?)</style>', page_data):
        css = match.strip().decode(encoding, 'replace')
        if not '\n' in css:
            continue
        css = css.replace('\r', '').replace('\n    ', '\n')
        with open_text(theme_dir, 'style.css') as f:
            f.write(css + '\n')
        return


class Index:

    def __init__(self, blog, body_class='index'):
        self.blog = blog
        self.body_class = body_class
        self.index = defaultdict(lambda: defaultdict(list))

    def add_post(self, post):
        self.index[post.tm.tm_year][post.tm.tm_mon].append(post)
        return self

    def save_index(self, index_dir='.', title=None):
        self.archives = sorted(((y, m) for y in self.index for m in self.index[y]),
            reverse=options.reverse_month
        )
        subtitle = self.blog.title if title else self.blog.subtitle
        title = title or self.blog.title
        with open_text(index_dir, dir_index) as idx:
            idx.write(self.blog.header(title, self.body_class, subtitle, True))
            if options.tag_index and self.body_class == 'index':
                idx.write('<p><a href=%s/%s>Tag index</a></p>\n' % (
                    tag_index_dir, dir_index
                ))
            for year in sorted(self.index.keys(), reverse=options.reverse_index):
                self.save_year(idx, index_dir, year)
            idx.write(u'<footer><p>Generated on %s by <a href=https://github.com/'
                'bbolli/tumblr-utils>tumblr-utils</a>.</p></footer>\n' % strftime('%x %X')
            )

    def save_year(self, idx, index_dir, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=options.reverse_index):
            tm = time.localtime(time.mktime([year, month, 3, 0, 0, 0, 0, 0, -1]))
            month_name = self.save_month(index_dir, year, month, tm)
            idx.write(u'    <li><a href=%s/%s title="%d post(s)">%s</a></li>\n' % (
                archive_dir, month_name, len(self.index[year][month]),
                strftime('%B', tm)
            ))
        idx.write('</ul>\n\n')

    def save_month(self, index_dir, year, month, tm):
        posts = sorted(self.index[year][month], key=lambda x: x.date, reverse=options.reverse_month)
        posts_month = len(posts)
        posts_page = options.posts_per_page if options.posts_per_page >= 1 else posts_month

        def pages_per_month(y, m):
            posts = len(self.index[y][m])
            return posts / posts_page + bool(posts % posts_page)

        def next_month(inc):
            i = self.archives.index((year, month))
            i += inc
            if i < 0 or i >= len(self.archives):
                return 0, 0
            return self.archives[i]

        FILE_FMT = '%d-%02d-p%s'
        pages_month = pages_per_month(year, month)
        for page, start in enumerate(range(0, posts_month, posts_page), start=1):

            archive = [self.blog.header(strftime('%B %Y', tm), body_class='archive')]
            archive.extend(p.get_post() for p in posts[start:start + posts_page])

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

        return first_file


class Indices:

    def __init__(self, blog):
        self.blog = blog
        self.main_index = Index(blog)
        self.tags = defaultdict(lambda: Index(blog, 'tag-archive'))

    def build_index(self):
        filter = join('*', dir_index) if options.dirs else '*' + post_ext
        self.all_posts = map(LocalPost, glob(path_to(post_dir, filter)))
        for post in self.all_posts:
            self.main_index.add_post(post)
            if options.tag_index:
                for tag, name in post.tags:
                    self.tags[tag].add_post(post).name = name

    def save_index(self):
        self.main_index.save_index()
        if options.tag_index:
            self.save_tag_index()

    def save_tag_index(self):
        global save_dir
        save_dir = '../../../'
        mkdir(path_to(tag_index_dir))
        self.fixup_media_links()
        tag_index = [self.blog.header('Tag index', 'tag-index', self.blog.title, True), '<ul>']
        for tag, index in sorted(self.tags.items(), key=lambda kv: kv[1].name):
            digest = hashlib.md5(tag).hexdigest()
            index.save_index(tag_index_dir + os.sep + digest,
                u"Tag ‛%s’" % index.name
            )
            tag_index.append(u'    <li><a href=%s/%s>%s</a></li>' % (
                digest, dir_index, escape(index.name)
            ))
        tag_index.extend(['</ul>', ''])
        with open_text(tag_index_dir, dir_index) as f:
            f.write(u'\n'.join(tag_index))

    def fixup_media_links(self):
        """Fixup all media links which now have to be two folders lower."""
        shallow_media = '../' + media_dir
        deep_media = save_dir + media_dir
        for p in self.all_posts:
            p.post = p.post.replace(shallow_media, deep_media)


class TumblrBackup:

    def __init__(self):
        self.errors = False
        self.total_count = 0

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
''' % (encoding, self.title, css_rel, body_class)
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

    def footer(self, base, previous_page, next_page, suffix):
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
            post_class = BlosxomPost
        else:
            save_folder = join(root_folder, options.outdir or account)
            media_folder = path_to(media_dir)
            if options.dirs:
                post_ext = ''
                save_dir = '../../'
                mkdir(path_to(post_dir), True)
            else:
                mkdir(save_folder, True)
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
                log(account, "Backing up posts after %d\r" % ident_max)
            except ValueError:  # max() arg is an empty sequence
                pass
        else:
            log(account, "Getting basic information\r")

        # start by calling the API with just a single post
        soup = apiparse(base, 1)
        if not soup:
            self.errors = True
            return

        # collect all the meta information
        resp = soup['response']
        if options.likes:
            _get_content = lambda soup: soup['response']['liked_posts']
            blog = {}
            last_post = resp['liked_count']
        else:
            _get_content = lambda soup: soup['response']['posts']
            blog = resp['blog']
            last_post = blog['posts']
        self.title = escape(blog.get('title', account))
        self.subtitle = blog.get('description', '')

        # use the meta information to create a HTML header
        TumblrPost.post_header = self.header(body_class='post')

        # find the post number limit to back up
        if options.count:
            last_post = min(last_post, options.count + options.skip)

        def _backup(posts):
            for p in sorted(posts, key=lambda x: x['id'], reverse=True):
                post = post_class(p)
                if ident_max and long(post.ident) <= ident_max:
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
                        elif 'trail' in p and 'is_current_item' not in p['trail'][-1]:
                            continue
                    elif 'trail' in p and p['trail'] and 'is_current_item' not in p['trail'][-1]:
                        continue
                backup_pool.add_work(post.save_content)
                self.post_count += 1
            return True

        # start the thread pool
        backup_pool = ThreadPool()
        try:
            # Get the JSON entries from the API, which we can only do for max 50 posts at once.
            # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
            last_batch = MAX_POSTS
            i = options.skip
            while i < last_post:
                # find the upper bound
                j = min(i + MAX_POSTS, last_post)
                log(account, "Getting posts %d to %d of %d\r" % (i, j - 1, last_post))

                soup = apiparse(base, j - i, i)
                if soup is None:
                    i += last_batch     # try the next batch
                    self.errors = True
                    continue

                posts = _get_content(soup)
                # posts can be empty if we don't backup reblogged posts
                if not posts or not _backup(posts):
                    break

                last_batch = len(posts)
                i += last_batch
        except:
            # ensure proper thread pool termination
            backup_pool.cancel()
            raise

        # wait until all posts have been saved
        backup_pool.wait()

        # postprocessing
        if not options.blosxom and self.post_count:
            get_avatar()
            get_style()
            if not have_custom_css:
                save_style()
            ix = Indices(self)
            ix.build_index()
            ix.save_index()

        log(account, "%d posts backed up\n" % self.post_count)
        self.total_count += self.post_count


class TumblrPost:

    post_header = ''    # set by TumblrBackup.backup()

    def __init__(self, post):
        self.content = ''
        self.post = post
        self.json_content = json.dumps(post, sort_keys=True, indent=4, separators=(',', ': '))
        self.creator = post['blog_name']
        self.ident = str(post['id'])
        self.url = post['post_url']
        self.shorturl = post['short_url']
        self.typ = str(post['type'])
        self.date = post['timestamp']
        self.isodate = datetime.utcfromtimestamp(self.date).isoformat() + 'Z'
        self.tm = time.localtime(self.date)
        self.title = ''
        self.tags = post['tags']
        self.note_count = post.get('note_count', 0)
        self.reblogged_from = post.get('reblogged_from_url')
        self.reblogged_root = post.get('reblogged_root_url')
        self.source_title = post.get('source_title', '')
        self.source_url = post.get('source_url', '')
        if options.request:
            self.tags_lower = set(t.lower() for t in self.tags)
        self.file_name = join(self.ident, dir_index) if options.dirs else self.ident + post_ext
        self.llink = self.ident if options.dirs else self.file_name

    def save_content(self):
        """generates the content for this post"""
        post = self.post
        content = []

        def append(s, fmt=u'%s'):
            content.append(fmt % s)

        def get_try(elt):
            return post.get(elt) or ''

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

        self.media_dir = join(post_dir, self.ident) if options.dirs else media_dir
        self.media_url = save_dir + self.media_dir
        self.media_folder = path_to(self.media_dir)

        if self.typ == 'text':
            self.title = get_try('title')
            append_try('body')

        elif self.typ == 'photo':
            url = get_try('link_url')
            is_photoset = len(post['photos']) > 1
            for offset, p in enumerate(post['photos'], start=1):
                o = p['original_size']
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
                    sys.stdout.write(u'Unable to download video in post #%s%-50s\n' %
                        (self.ident, ' ')
                    )
            if src:
                append(u'<p><video controls><source src="%s" type=video/mp4>%s<br>\n<a href="%s">%s</a></video></p>' % (
                    src, "Your browser does not support the video element.", src, "Video file"
                ))
            else:
                append(post['player'][-1]['embed_code'])
            append_try('caption')

        elif self.typ == 'audio':
            src = ''
            if options.save_audio:
                audio_url = get_try('audio_url') or get_try('audio_source_url')
                if post['audio_type'] == 'tumblr':
                    if audio_url.startswith('https://a.tumblr.com/'):
                        src = self.get_media_url(audio_url, '.mp3')
                    elif audio_url.startswith('https://www.tumblr.com/audio_file/'):
                        audio_url = u'https://a.tumblr.com/%so1.mp3' % audio_url.split('/')[-1]
                        src = self.get_media_url(audio_url, '.mp3')
                elif post['audio_type'] == 'soundcloud':
                    src = self.get_media_url(audio_url, '.mp3')
            if src:
                append(u'<p><audio controls><source src="%s" type=audio/mpeg>%s<br>\n<a href="%s">%s</a></audio></p>' % (
                    src, "Your browser does not support the audio element.", src, "Audio file"
                ))
            else:
                append(post['player'])
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
            sys.stderr.write(
                u"Unknown post type '%s' in post #%s%-50s\n" % (self.typ, self.ident, ' ')
            )
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
            'ignoreerrors': True
        }
        if options.cookiefile:
            ydl_options['cookiefile'] = options.cookiefile
        ydl = youtube_dl.YoutubeDL(ydl_options)
        ydl.add_default_info_extractors()
        try:
            result = ydl.extract_info(youtube_url, download=False)
            media_filename = sanitize_filename(filetmpl % result['entries'][0], restricted=True)
        except:
            return ''

        # check if a file with this name already exists
        if not os.path.isfile(media_filename):
            try:
                ydl.extract_info(youtube_url, download=True)
            except:
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

        def _addexif(fn):
            if options.exif and fn.endswith('.jpg'):
                add_exif(fn, set(self.tags))

        image_filename = self.get_filename(image_url, '_o%s' % offset if offset else '')
        saved_name = self.download_media(image_url, image_filename)
        if saved_name is not None:
            _addexif(join(self.media_folder, saved_name))
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
        image_url = match.group(2)
        if image_url.startswith('//'):
            image_url = 'http:' + image_url
        image_url = self.maxsize_image_url(image_url)
        path = urlparse.urlparse(image_url).path
        image_filename = path.split('/')[-1]
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
        poster_url = match.group(2)
        if poster_url.startswith('//'):
            poster_url = 'http:' + poster_url
        path = urlparse.urlparse(poster_url).path
        poster_filename = path.split('/')[-1]
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
        video_url = match.group(2)
        if video_url.startswith('//'):
            video_url = 'http:' + video_url
        path = urlparse.urlparse(video_url).path
        video_filename = path.split('/')[-1]
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
        elif options.image_names == 'bi':
            return account + '_' + self.ident + offset
        else:
            # delete characters not allowed under Windows
            return re.sub(r'[:<>"/\\|*?]', '', url.split('/')[-1])

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
            resp = urlopen(url)
            with open_media(self.media_dir, filename) as dest:
                data = resp.read(HTTP_CHUNK_SIZE)
                hdr = data[:32]     # save the first few bytes
                while data:
                    dest.write(data)
                    data = resp.read(HTTP_CHUNK_SIZE)
        except (EnvironmentError, ValueError, HTTPException) as e:
            sys.stderr.write('%s downloading %s\n' % (e, url))
            try:
                os.unlink(path_to(self.media_dir, filename))
            except OSError as e:
                if e.errno != errno.ENOENT:
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
        typ = ('liked-' if options.likes else '') + self.typ
        post = self.post_header + u'<article class=%s id=p-%s>\n' % (typ, self.ident)
        post += u'<header>\n'
        if options.likes:
            post += u'<p><a href=\"http://{0}.tumblr.com/\" class=\"tumblr_blog\">{0}</a>:</p>\n'.format(self.creator)
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
        post += '\n</article>\n'
        return post

    @staticmethod
    def tag_link(tag):
        tag_disp = escape(TAG_FMT % tag)
        if not TAGLINK_FMT:
            return tag_disp + ' '
        url = TAGLINK_FMT % {'domain': blog_name, 'tag': urllib.quote(tag.encode('utf-8'))}
        return u'<a href=%s>%s</a>\n' % (url, tag_disp)

    def save_post(self):
        """saves this post locally"""
        if options.dirs:
            f = open_text(post_dir, self.ident, dir_index)
        else:
            f = open_text(post_dir, self.file_name)
        with f:
            f.write(self.get_post())
        os.utime(f.stream.name, (self.date, self.date))  # XXX: is f.stream.name portable?
        if options.json:
            with open_text(json_dir, self.ident + '.json') as f:
                f.write(self.json_content)


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


class LocalPost:

    def __init__(self, post_file):
        with codecs.open(post_file, 'r', encoding) as f:
            post = f.read()
        # extract all URL-encoded tags
        self.tags = []
        footer_pos = post.find('<footer>')
        if footer_pos > 0:
            self.tags = re.findall(r'(?m)<a.+?/tagged/(.+?)>#(.+?)</a>', post[footer_pos:])
        # remove header and footer
        lines = post.split('\n')
        while lines and '<article ' not in lines[0]:
            del lines[0]
        while lines and '</article>' not in lines[-1]:
            del lines[-1]
        self.post = '\n'.join(lines)
        parts = post_file.split(os.sep)
        if parts[-1] == dir_index:  # .../<post_id>/index.html
            self.file_name = os.sep.join(parts[-2:])
            self.ident = parts[-2]
        else:
            self.file_name = parts[-1]
            self.ident = splitext(self.file_name)[0]
        self.date = os.stat(post_file).st_mtime
        self.tm = time.localtime(self.date)

    def get_post(self):
        return self.post


class ThreadPool:

    def __init__(self, thread_count=20, max_queue=1000):
        self.queue = Queue.Queue(max_queue)
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
            log('', "\rStopping threads %s%s\r" %
                (' ' * i, '.' * (len(self.threads) - i))
            )
            t.join()

    def handler(self):
        while not self.abort.is_set():
            try:
                work = self.queue.get(True, 0.1)
            except Queue.Empty:
                if self.quit.is_set():
                    break
            else:
                if self.quit.is_set() and self.queue.qsize() % MAX_POSTS == 0:
                    log(account, "%d remaining posts to save\r" % self.queue.qsize())
                try:
                    work()
                finally:
                    self.queue.task_done()


if __name__ == '__main__':
    import optparse

    def csv_callback(option, opt, value, parser):
        setattr(parser.values, option.dest, set(value.split(',')))

    def tags_callback(option, opt, value, parser):
        request_callback(option, opt, TYPE_ANY + ':' + value.replace(',', ':'), parser)

    def request_callback(option, opt, value, parser):
        request = parser.values.request or {}
        for req in value.lower().split(','):
            parts = req.strip().split(':')
            typ = parts.pop(0)
            if typ != TYPE_ANY and typ not in POST_TYPES:
                parser.error("%s: invalid post type '%s'" % (opt, typ))
            for typ in POST_TYPES if typ == TYPE_ANY else (typ,):
                if parts:
                    request[typ] = request.get(typ, set()).union(parts)
                else:
                    request[typ] = set([TAG_ANY])
        parser.values.request = request

    parser = optparse.OptionParser("Usage: %prog [options] blog-name ...",
        description="Makes a local backup of Tumblr blogs."
    )
    parser.add_option('-O', '--outdir', help="set the output directory"
        " (default: blog-name)"
    )
    parser.add_option('-D', '--dirs', action='store_true',
        help="save each post in its own folder"
    )
    parser.add_option('-q', '--quiet', action='store_true',
        help="suppress progress messages"
    )
    parser.add_option('-i', '--incremental', action='store_true',
        help="incremental backup mode"
    )
    parser.add_option('-l', '--likes', action='store_true',
        dest='likes', help="save a blog's likes, not its posts"
    )
    parser.add_option('-k', '--skip-images', action='store_false', default=True,
        dest='save_images', help="do not save images; link to Tumblr instead"
    )
    parser.add_option('--save-video', action='store_true', help="save all video files")
    parser.add_option('--save-video-tumblr', action='store_true', help="save only Tumblr video files")
    parser.add_option('--save-audio', action='store_true', help="save audio files")
    parser.add_option('--cookiefile', help="cookie file for youtube-dl")
    parser.add_option('-j', '--json', action='store_true',
        help="save the original JSON source"
    )
    parser.add_option('-b', '--blosxom', action='store_true',
        help="save the posts in blosxom format"
    )
    parser.add_option('-r', '--reverse-month', action='store_false', default=True,
        help="reverse the post order in the monthly archives"
    )
    parser.add_option('-R', '--reverse-index', action='store_false', default=True,
        help="reverse the index file order"
    )
    parser.add_option('--tag-index', action='store_true',
        help="also create an archive per tag"
    )
    parser.add_option('-a', '--auto', type='int', metavar="HOUR",
        help="do a full backup at HOUR hours, otherwise do an incremental backup"
        " (useful for cron jobs)"
    )
    parser.add_option('-n', '--count', type='int', default=0,
        help="save only COUNT posts"
    )
    parser.add_option('-s', '--skip', type='int', default=0,
        help="skip the first SKIP posts"
    )
    parser.add_option('-p', '--period', help="limit the backup to PERIOD"
        " ('y', 'm', 'd' or YYYY[MM[DD]])"
    )
    parser.add_option('-N', '--posts-per-page', type='int', default=50,
        metavar='COUNT', help="set the number of posts per monthly page, "
        "0 for unlimited"
    )
    parser.add_option('-Q', '--request', type='string', action='callback',
        callback=request_callback, help="save posts matching the request"
        u" TYPE:TAG:TAG:…,TYPE:TAG:…,…. TYPE can be %s or %s; TAGs can be"
        " omitted or a colon-separated list. Example: -Q %s:personal,quote"
        ",photo:me:self" % (', '.join(POST_TYPES), TYPE_ANY, TYPE_ANY)
    )
    parser.add_option('-t', '--tags', type='string', action='callback',
        callback=tags_callback, help="save only posts tagged TAGS (comma-separated values;"
        " case-insensitive)"
    )
    parser.add_option('-T', '--type', type='string', action='callback',
        callback=request_callback, help="save only posts of type TYPE"
        " (comma-separated values from %s)" % ', '.join(POST_TYPES)
    )
    parser.add_option('--no-reblog', action='store_true', help="don't save reblogged posts")
    parser.add_option('-I', '--image-names', type='choice', choices=('o', 'i', 'bi'),
        default='o', metavar='FMT',
        help="image filename format ('o'=original, 'i'=<post-id>, 'bi'=<blog-name>_<post-id>)"
    )
    parser.add_option('-e', '--exif', type='string', action='callback',
        callback=csv_callback, default=set(), metavar='KW',
        help="add EXIF keyword tags to each picture (comma-separated values;"
        " '-' to remove all tags, '' to add no extra tags)"
    )
    parser.add_option('-S', '--no-ssl-verify', action='store_true',
        help="ignore SSL verification errors"
    )
    options, args = parser.parse_args()

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

    args = args or DEFAULT_BLOGS
    if not args:
        parser.error("Missing blog-name")
    if options.outdir and len(args) > 1:
        parser.error("-O can only be used for a single blog-name")
    if options.dirs and options.tag_index:
        parser.error("-D cannot be used with --tag-index")
    if options.exif and not pyexiv2:
        parser.error("--exif: module 'pyexif2' is not installed")
    if options.save_video and not youtube_dl:
        parser.error("--save-video: module 'youtube_dl' is not installed")

    tb = TumblrBackup()
    try:
        for account in args:
            tb.backup(account)
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPT)

    sys.exit(tb.exit_code())
