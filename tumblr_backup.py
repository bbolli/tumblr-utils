#!/usr/bin/env python
# encoding: utf-8

# standard Python library imports
from __future__ import with_statement
import codecs
from collections import defaultdict
import errno
from glob import glob
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
import sys
import threading
import time
import urllib
import urllib2
from xml.sax.saxutils import escape

# extra optional packages
try:
    import pyexiv2
except ImportError:
    pyexiv2 = None

# default blog name(s)
DEFAULT_BLOGS = ['bbolli']

# Format of displayed tags
TAG_FMT = '#%s'

# Format of tag link URLs; set to None to suppress the links.
# Named placeholders that will be replaced: domain, tag
TAGLINK_FMT = 'http://%(domain)s/tagged/%(tag)s'


# add another JPEG recognizer
# see http://www.garykessler.net/library/file_sigs.html
def test_jpg(h, f):
    if h[:3] == '\xFF\xD8\xFF' and h[3] in "\xDB\xE0\xE1\xE2\xE3":
        return 'jpg'

imghdr.tests.append(test_jpg)

# variable directory names, will be set in TumblrBackup.backup()
save_folder = ''
image_folder = ''

# constant names
root_folder = os.getcwdu()
post_dir = 'posts'
json_dir = 'json'
image_dir = 'images'
archive_dir = 'archive'
theme_dir = 'theme'
save_dir = '../'
backup_css = 'backup.css'
custom_css = 'custom.css'
avatar_base = 'avatar'
dir_index = 'index.html'

blog_name = ''
post_ext = '.html'
have_custom_css = False

POST_TYPES = (
    'text', 'quote', 'link', 'answer', 'video', 'audio', 'photo', 'chat'
)
POST_TYPES_SET = frozenset(POST_TYPES)
POST_TYPES_AND_ANY_SET = frozenset(POST_TYPES +('any',))

MAX_POSTS = 50

HTTP_TIMEOUT = 30

# bb-tumblr-backup API key
API_KEY = '8YUsKJvcJxo2MDwmWMDiXZGuMuIbeCwuQGP5ZHSEA4jBJPMnJT'

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass
encoding = 'utf-8'
time_encoding = locale.getlocale(locale.LC_TIME)[1] or encoding


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


def open_image(*parts):
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
    return 'http://api.tumblr.com/v2/blog/' + blog_name + '/posts'


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
    params = {'api_key': API_KEY, 'limit': count}
    if start > 0:
        params['offset'] = start
    url = base + '?' + urllib.urlencode(params)
    for _ in range(10):
        try:
            resp = urllib2.urlopen(url, timeout=HTTP_TIMEOUT)
            data = resp.read()
        except IOError as e:
            sys.stderr.write('%s getting %s\n' % (e, url))
            continue
        if resp.info().gettype() == 'application/json':
            break
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
    except:
        sys.stderr.write('Error reading metadata for image %s\n' % image_name)
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
    except:
        sys.stderr.write('Writing metadata failed for tags: %s in: %s\n' % (tags, image_name))


def save_style():
    with open_text(backup_css) as css:
        css.write('''\
body { width: 720px; margin: 0 auto; }
img { max-width: 720px; }
blockquote { margin-left: 0; border-left: 8px #999 solid; padding: 0 24px; }
.archive h1, .subtitle, article { padding-bottom: 0.75em; border-bottom: 1px #ccc dotted; }
.post a.llink { display: none; }
.meta a { text-decoration: none; }
body > img { float: right; }
article footer, article footer a { font-size: small; color: #999; text-decoration: none; }
body > footer { padding: 1em 0; }
''')


def get_avatar():
    try:
        resp = urllib2.urlopen('http://api.tumblr.com/v2/blog/%s/avatar' % blog_name,
            timeout=HTTP_TIMEOUT
        )
        avatar_data = resp.read()
    except:
        return
    avatar_file = avatar_base + '.' + imghdr.what(None, avatar_data[:32])
    with open_image(theme_dir, avatar_file) as f:
        f.write(avatar_data)


def get_style():
    """Get the blog's CSS by brute-forcing it from the home page.
    The v2 API has no method for getting the style directly.
    See https://groups.google.com/d/msg/tumblr-api/f-rRH6gOb6w/sAXZIeYx5AUJ"""
    try:
        resp = urllib2.urlopen('http://%s/' % blog_name, timeout=HTTP_TIMEOUT)
        page_data = resp.read()
    except:
        return
    match = re.search(r'(?s)<style type=.text/css.>(.*?)</style>', page_data)
    if match:
        css = match.group(1).strip().decode(encoding, 'replace')
        if not css:
            return
        css = css.replace('\r', '').replace('\n    ', '\n')
        with open_text(theme_dir, 'style.css') as f:
            f.write(css + '\n')


class TumblrBackup:

    def __init__(self):
        self.total_count = 0
        self.index = defaultdict(lambda: defaultdict(list))
        self.archives = []

    def build_index(self):
        filter = join('*', dir_index) if options.dirs else '*' + post_ext
        for f in glob(path_to(post_dir, filter)):
            post = LocalPost(f)
            self.index[post.tm.tm_year][post.tm.tm_mon].append(post)
        self.archives = sorted(((y, m) for y in self.index for m in self.index[y]),
            reverse=options.reverse_month
        )

    def save_index(self):
        f = glob(path_to(theme_dir, avatar_base + '.*'))
        avatar = split(f[0])[1] if f else None
        with open_text(dir_index) as idx:
            idx.write(self.header(self.title, body_class='index',
                subtitle=self.subtitle, avatar=avatar
            ))
            for year in sorted(self.index.keys(), reverse=options.reverse_index):
                self.save_year(idx, year)
            idx.write(u'<p>Generated on %s.</p>\n' % strftime('%x %X'))

    def save_year(self, idx, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=options.reverse_index):
            tm = time.localtime(time.mktime([year, month, 3, 0, 0, 0, 0, 0, -1]))
            month_name = self.save_month(year, month, tm)
            idx.write(u'    <li><a href=%s/%s title="%d post(s)">%s</a></li>\n' % (
                archive_dir, month_name, len(self.index[year][month]),
                strftime('%B', tm)
            ))
        idx.write('</ul>\n\n')

    def save_month(self, year, month, tm):
        posts = sorted(self.index[year][month], key=lambda x: x.date, reverse=options.reverse_month)
        posts_month = len(posts)
        posts_page = options.posts_per_page if options.posts_per_page >= 1 else posts_month

        def pages_per_month(y, m):
            posts = len(self.index[y][m])
            return posts / posts_page + bool(posts % posts_page)

        def next_month(previous):
            i = self.archives.index((year, month))
            i += -1 if previous else 1
            if i < 0 or i >= len(self.archives):
                return 0, 0
            return self.archives[i]

        pages_month = pages_per_month(year, month)
        for page, start in enumerate(range(0, posts_month, posts_page), start=1):

            archive = [self.header(strftime('%B %Y', tm), body_class='archive')]
            archive.extend(p.get_post() for p in posts[start:start + posts_page])

            file_name = '%d-%02d-p%s' % (year, month, page)
            if options.dirs:
                base = save_dir + archive_dir + '/'
                suffix = '/'
                arch = open_text(archive_dir, file_name, dir_index)
                file_name += suffix
            else:
                base = ''
                suffix = post_ext
                file_name += suffix
                arch = open_text(archive_dir, file_name)

            if page > 1:
                pp = '%d-%02d-p%s' % (year, month, page - 1)
            else:
                py, pm = next_month(True)
                pp = '%d-%02d-p%s' % (py, pm, pages_per_month(py, pm)) if py else ''
                first_file = file_name

            if page < pages_month:
                np = '%d-%02d-p%s' % (year, month, page + 1)
            else:
                ny, nm = next_month(False)
                np = '%d-%02d-p%s' % (ny, nm, 1) if ny else ''

            archive.append(self.footer(base, pp, np, suffix))

            with arch:
                arch.write('\n'.join(archive))

        return first_file

    def header(self, title='', body_class='', subtitle='', avatar=''):
        root_rel = '' if body_class == 'index' else save_dir
        css_rel = root_rel + (custom_css if have_custom_css else backup_css)
        if body_class:
            body_class = ' class=' + body_class
        h = u'''<!DOCTYPE html>

<meta charset=%s>
<title>%s</title>
<link rel=stylesheet href=%s>

<body%s>

''' % (encoding, self.title, css_rel, body_class)
        if avatar:
            h += '<img src=%s%s/%s alt=Avatar>\n' % (root_rel, theme_dir, avatar)
        if title:
            h += u'<h1>%s</h1>\n' % title
        if subtitle:
            h += u'<p class=subtitle>%s</p>\n' % subtitle
        return h

    def footer(self, base, previous_page, next_page, suffix):
        f = '<footer>'
        f += '<a href=%s rel=index>Index</a>\n' % save_dir
        if previous_page:
            f += '| <a href=%s%s%s rel=prev>Previous</a>\n' % (base, previous_page, suffix)
        if next_page:
            f += '| <a href=%s%s%s rel=next>Next</a>\n'% (base, next_page, suffix)
        f += '</footer>\n'
        return f

    def backup(self, account):
        """makes single files and an index for every post on a public Tumblr blog account"""

        base = get_api_url(account)

        # make sure there are folders to save in
        global save_folder, image_folder, post_ext, post_dir, save_dir, have_custom_css
        if options.blosxom:
            save_folder = root_folder
            post_ext = '.txt'
            post_dir = os.curdir
            post_class = BlosxomPost
        else:
            save_folder = join(root_folder, options.outdir or account)
            image_folder = path_to(image_dir)
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
            return

        # collect all the meta information
        blog = soup['response']['blog']
        try:
            self.title = escape(blog['title'])
        except KeyError:
            self.title = account
        self.subtitle = blog['description']

        # use the meta information to create a HTML header
        TumblrPost.post_header = self.header(body_class='post')

        # find the post number limit to back up
        last_post = blog['posts']
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
                    if ((post.typ in options.request) or ('any' in options.request)):
                        if post.typ in options.request:
                            if ((len(options.request[post.typ])) and (not set(options.request[post.typ]) & post.tags_lower)):
                                if 'any' in options.request:
                                    if ((len(options.request['any'])) and (not set(options.request['any']) & post.tags_lower)):
                                        continue
                                else:
                                    continue
                        else:
                            if ((len(options.request['any'])) and (not set(options.request['any']) & post.tags_lower)):
                                continue
                    else:
                        continue
                if options.tags and not options.tags & post.tags_lower:
                    continue
                if options.type and post.typ not in options.type:
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
                    continue

                posts = soup['response']['posts']
                if not _backup(posts):
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
            self.build_index()
            self.save_index()

        log(account, "%d posts backed up\n" % self.post_count)
        self.total_count += self.post_count


class TumblrPost:

    post_header = ''    # set by TumblrBackup.backup()

    def __init__(self, post):
        self.content = ''
        self.post = post
        self.json_content = json.dumps(post, sort_keys=True, indent=4, separators=(',', ': '))
        self.ident = str(post['id'])
        self.url = post['post_url']
        self.typ = post['type']
        self.date = post['timestamp']
        self.tm = time.localtime(self.date)
        self.title = ''
        self.tags = post['tags']
        self.note_count = post.get('note_count', 0)
        self.source_title = post.get('source_title', '')
        self.source_url = post.get('source_url', '')
        if options.tags or options.request:
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
            return post.get(elt)

        def append_try(elt, fmt=u'%s'):
            elt = get_try(elt)
            if elt:
                if options.save_images:
                    elt = re.sub(r'''(?i)(<img [^>]*\bsrc\s*=\s*["'])(.*?)(["'][^>]*>)''',
                        self.get_inline_image, elt
                    )
                append(elt, fmt)

        self.image_dir = join(post_dir, self.ident) if options.dirs else image_dir
        self.images_url = save_dir + self.image_dir
        self.image_folder = path_to(self.image_dir)

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
            append(post['player'][-1]['embed_code'])
            append_try('caption')

        elif self.typ == 'audio':
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

        # fix wrongly nested HTML tags
        for p in ('<p>(<(%s)>)', '(</(%s)>)</p>'):
            self.content = re.sub(p % 'p|ol|iframe[^>]*', r'\1', self.content)

        self.save_post()

    def get_image_url(self, image_url, offset):
        """Saves an image if not saved yet. Returns the new URL or
        the original URL in case of download errors."""

        def _addexif(fn):
            if options.exif and fn.endswith('.jpg'):
                add_exif(fn, set(self.tags))

        # determine the image file name
        offset = '_o%s' % offset if offset else ''
        if options.image_names == 'i':
            image_filename = self.ident + offset
        elif options.image_names == 'bi':
            image_filename = account + '_' + self.ident + offset
        else:
            image_filename = image_url.split('/')[-1]

        saved_name = self.download_image(image_url, image_filename)
        if saved_name is not None:
            _addexif(join(self.image_folder, saved_name))
            image_url = u'%s/%s' % (self.images_url, saved_name)
        return image_url

    def get_inline_image(self, match):
        """Saves an inline image if not saved yet. Returns the new <img> tag or
        the original one in case of download errors."""

        image_url = match.group(2)
        image_filename = image_url.split('/')[-1]

        saved_name = self.download_image(image_url, image_filename)
        if saved_name is None:
            return match.group(0)
        return u'%s%s/%s%s' % (match.group(1), self.images_url,
            saved_name, match.group(3)
        )

    def download_image(self, image_url, image_filename):
        # check if a file with this name already exists
        known_extension = '.' in image_filename[-5:]
        image_glob = glob(join(self.image_folder, image_filename +
            ('' if known_extension else '.*')
        ))
        if image_glob:
            return split(image_glob[0])[1]
        # download the image data
        try:
            image_response = urllib2.urlopen(image_url, timeout=HTTP_TIMEOUT)
            image_data = image_response.read()
            image_response.close()
        except IOError:
            return None
        # determine the file type if it's unknown
        if not known_extension:
            image_type = imghdr.what(None, image_data[:32])
            if image_type:
                image_filename += '.' + image_type.replace('jpeg', 'jpg')
        # save the image
        with open_image(self.image_dir, image_filename) as image_file:
            image_file.write(image_data)
        return image_filename

    def get_post(self):
        """returns this post in HTML"""
        post = self.post_header + u'<article class=%s id=p-%s>\n' % (self.typ, self.ident)
        post += u'<p class=meta><span class=date>%s</span>\n' % strftime('%x %X', self.tm)
        post += u'<a class=llink href=%s%s/%s>¶</a>\n' % (save_dir, post_dir, self.llink)
        post += u'<a href=%s rel=canonical>●</a></p>\n' % self.url
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
            self.lines = f.readlines()
        # remove header and footer
        while self.lines and '<article ' not in self.lines[0]:
            del self.lines[0]
        while self.lines and '</article>' not in self.lines[-1]:
            del self.lines[-1]
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
        return u''.join(self.lines)


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
            log('', '\rStopping threads %s%s\r' %
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
                work()
                self.queue.task_done()


if __name__ == '__main__':
    import optparse

    def csv_callback(option, opt, value, parser):
        setattr(parser.values, option.dest, set(value.split(',')))

    def tags_callback(option, opt, value, parser):
        csv_callback(option, opt, value.lower(), parser)

    def type_callback(option, opt, value, parser):
        types = set(value.lower().split(','))
        if not types <= POST_TYPES_SET:
            parser.error("--type: invalid post types")
        setattr(parser.values, option.dest, types)
    def request_callback(option, opt, value, parser):
        raw_request = value.lower().split(';')
        request = {}
        for elt in raw_request:
            if ':' in elt:
                request.setdefault(elt.split(':')[0], elt.split(':')[1].split(','))
            else:
                request.setdefault(elt, '')
        if not set(request.keys()) <= POST_TYPES_AND_ANY_SET:
            parser.error("--request: invalid post types")
        setattr(parser.values, option.dest, request)
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
    parser.add_option('-k', '--skip-images', action='store_false', default=True,
        dest='save_images', help="do not save images; link to Tumblr instead"
    )
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
        metavar='COUNT', help="set the number of posts per monthly page"
    )   
    parser.add_option('-Q', '--request', type='string', action='callback',
        callback=request_callback, help="save posts following the pattern TYPE:TAGS."
        " TYPE can be any, %s and TAGS can be omitted."
        " (TAGS can be comma-separated values, pattern semicolon-separated values)"
        " Example: \"any:personal;quote;photo:me,self\""  % ', '.join(POST_TYPES)
    )
    parser.add_option('-t', '--tags', type='string', action='callback',
        callback=tags_callback, help="save only posts tagged TAGS (comma-separated values;"
        " case-insensitive)"
    )
    parser.add_option('-T', '--type', type='string', action='callback',
        callback=type_callback, help="save only posts of type TYPE"
        " (comma-separated values from %s)" % ', '.join(POST_TYPES)
    )
    parser.add_option('-I', '--image-names', type='choice', choices=('o', 'i', 'bi'),
        default='o', metavar='FMT',
        help="image filename format ('o'=original, 'i'=<post-id>, 'bi'=<blog-name>_<post-id>)"
    )
    parser.add_option('-e', '--exif', type='string', action='callback',
        callback=csv_callback, default=set(), metavar='KW',
        help="add EXIF keyword tags to each picture (comma-separated values;"
        " '-' to remove all tags, '' to add no extra tags)"
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
    if not args:
        args = DEFAULT_BLOGS
    if options.outdir and len(args) > 1:
        parser.error("-O can only be used for a single blog-name")
    if options.exif and not pyexiv2:
        parser.error("--exif: module 'pyexif2' is not installed")

    tb = TumblrBackup()
    try:
        for account in args:
            tb.backup(account)
    except KeyboardInterrupt:
        sys.exit(3)

    sys.exit(0 if tb.total_count else 1)
