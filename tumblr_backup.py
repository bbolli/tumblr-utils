#!/usr/bin/env python
# encoding: utf-8

# standard Python library imports
from __future__ import with_statement
import os
import sys
import urllib
import urllib2
from xml.sax.saxutils import escape
from xml.sax import SAXException
import codecs
import imghdr
from collections import defaultdict
import time
import locale
from glob import glob
import re

# extra required packages
import xmltramp

join = os.path.join

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
xml_dir = 'xml'
image_dir = 'images'
archive_dir = 'archive'
theme_dir = 'theme'
backup_css = 'backup.css'
custom_css = 'custom.css'
avatar_base = 'avatar'

blog_name = ''
post_header = ''
post_ext = '.html'
have_custom_css = False

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
        if recursive:
            os.makedirs(dir)
        else:
            os.mkdir(dir)

def path_to(*parts):
    return join(save_folder, *parts)

def open_file(open_fn, parts):
    if len(parts) > 1:
        mkdir(path_to(*parts[:-1]))
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
    base = 'http://' + blog_name + '/api/read'
    if options.private:
        password_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(None, base, '', options.private)
        auth_manager = urllib2.HTTPBasicAuthHandler(password_manager)
        opener = urllib2.build_opener(auth_manager)
        urllib2.install_opener(opener)
    return base

def xmlparse(url, data=None):
    for _ in range(10):
        try:
            resp = urllib2.urlopen(url, data)
        except (urllib2.URLError, urllib2.HTTPError) as e:
            sys.stderr.write('%s getting %s\n' % (e, url))
            continue
        if resp.info().gettype() == 'text/xml':
            break
    else:
        return None
    xml = resp.read()
    try:
        doc = xmltramp.parse(xml)
    except SAXException as e:
        sys.stderr.write('%s %r\n\n%r\n\n%s\n' % (resp.info().gettype(), resp.msg, e, xml))
        return None
    return doc if doc._name == 'tumblr' else None

def save_image(image_url):
    """saves an image if not saved yet, returns the local file name"""
    def _url(fn):
        return u'../%s/%s' % (image_dir, fn)
    image_filename = image_url.split('/')[-1]
    glob_filter = '' if '.' in image_filename else '.*'
    # check if a file with this name already exists
    image_glob = glob(join(image_folder, image_filename + glob_filter))
    if image_glob:
        return _url(os.path.split(image_glob[0])[1])
    # download the image data
    try:
        image_response = urllib2.urlopen(image_url)
    except urllib2.HTTPError:
        # return the original URL
        return image_url
    image_data = image_response.read()
    image_response.close()
    # determine the file type if it's unknown
    if '.' not in image_filename:
        image_type = imghdr.what(None, image_data[:32])
        if image_type:
            image_filename += '.' + image_type.replace('jpeg', 'jpg')
    # save the image
    with open_image(image_dir, image_filename) as image_file:
        image_file.write(image_data)
    return _url(image_filename)

def save_style():
    with open_text(backup_css) as css:
        css.write('''\
body { width: 720px; margin: 0 auto; }
img { max-width: 720px; }
blockquote { margin-left: 0; border-left: 8px #999 solid; padding: 0 24px; }
.archive h1, .subtitle, article { padding-bottom: 0.75em; border-bottom: 1px #ccc dotted; }
.post a.llink { display: none; }
.meta a { text-decoration: none; }
.avatar { float: right; }
''')

def header(heading, title='', body_class='', subtitle='', avatar=''):
    root_rel = '' if body_class == 'index' else '../'
    css_rel = root_rel + (custom_css if have_custom_css else backup_css)
    if body_class:
        body_class = ' class=' + body_class
    h = u'''<!DOCTYPE html>

<meta charset=%s>
<title>%s</title>
<link rel=stylesheet href=%s>

<body%s>

''' % (encoding, heading, css_rel, body_class)
    if avatar:
        h += '<img src=%s%s/%s alt=Avatar class=avatar>\n' % (root_rel, theme_dir, avatar)
    if title:
        h += u'<h1>%s</h1>\n' % title
    if subtitle:
        h += u'<p class=subtitle>%s</p>\n' % subtitle
    return h

def get_avatar():
    try:
        resp = urllib2.urlopen('http://api.tumblr.com/v2/blog/%s/avatar' % blog_name)
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
        resp = urllib2.urlopen('http://%s/' % blog_name)
        page_data = resp.read()
    except:
        return
    match = re.search(r'(?s)<style type=.text/css.>(.*?)</style>', page_data)
    if match:
        css = match.group(1).strip()
        if not css:
            return
        css = css.replace('\r', '').replace('\n    ', '\n')
        with open_text(theme_dir, 'style.css') as f:
            f.write(css + '\n')


class TumblrBackup:

    def __init__(self):
        self.total_count = 0

    def build_index(self):
        for f in glob(path_to(post_dir, '*.html')):
            post = LocalPost(f)
            self.index[post.tm.tm_year][post.tm.tm_mon].append(post)

    def save_index(self):
        f = glob(path_to(theme_dir, avatar_base + '.*'))
        avatar = os.path.split(f[0])[1] if f else None
        with open_text('index.html') as idx:
            idx.write(header(self.title, self.title, body_class='index',
                subtitle=self.subtitle, avatar=avatar
            ))
            for year in sorted(self.index.keys(), reverse=options.reverse_index):
                self.save_year(idx, year)
            idx.write('<p>Generated on %s.</p>\n' % strftime('%x %X'))

    def save_year(self, idx, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=options.reverse_index):
            tm = time.localtime(time.mktime([year, month, 3, 0, 0, 0, 0, 0, -1]))
            month_name = self.save_month(year, month, tm)
            idx.write('    <li><a href=%s/%s title="%d post(s)">%s</a></li>\n' % (
                archive_dir, month_name, len(self.index[year][month]),
                strftime('%B', tm)
            ))
        idx.write('</ul>\n\n')

    def save_month(self, year, month, tm):
        file_name = '%d-%02d.html' % (year, month)
        with open_text(archive_dir, file_name) as arch:
            arch.write('\n\n'.join([
                header(self.title, strftime('%B %Y', tm), body_class='archive'),
                '\n'.join(p.get_post() for p in sorted(
                    self.index[year][month], key=lambda x: x.date, reverse=options.reverse_month
                )),
                '<p><a href=../ rel=contents>Index</a></p>\n'
            ]))
        return file_name

    def backup(self, account):
        """makes single files and an index for every post on a public Tumblr blog account"""

        base = get_api_url(account)

        # make sure there are folders to save in
        global save_folder, image_folder, post_ext, post_dir, have_custom_css
        if options.blosxom:
            save_folder = root_folder
            post_ext = '.txt'
            post_dir = os.curdir
            post_class = BlosxomPost
        else:
            save_folder = join(root_folder, account)
            image_folder = path_to(image_dir)
            post_class = TumblrPost
            have_custom_css = os.access(path_to(custom_css), os.R_OK)
        mkdir(save_folder, True)

        self.post_count = 0

        # prepare the period start and end timestamps
        if options.period:
            i = 0; tm = [int(options.period[:4]), 1, 1, 0, 0, 0, 0, 0, -1]
            if len(options.period) >= 6:
                i = 1; tm[1] = int(options.period[4:6])
            if len(options.period) == 8:
                i = 2; tm[2] = int(options.period[6:8])
            p_start = time.mktime(tm)
            tm[i] += 1
            p_stop = time.mktime(tm)

        # get the highest post id already saved
        ident_max = None
        if options.incremental:
            try:
                ident_max = max(
                    long(os.path.splitext(os.path.split(f)[1])[0])
                    for f in glob(path_to(post_dir, '*' + post_ext))
                )
                log(account, "Backing up posts after %d\r" % ident_max)
            except ValueError:  # max() arg is an empty sequence
                pass
        else:
            log(account, "Getting basic information\r")

        # start by calling the API with just a single post
        soup = xmlparse(base + '?num=1')
        if not soup:
            return

        # collect all the meta information
        tumblelog = soup.tumblelog
        try:
            self.title = escape(tumblelog('title'))
        except KeyError:
            self.title = account
        self.subtitle = unicode(tumblelog)

        # use the meta information to create a HTML header
        global post_header
        post_header = header(self.title, body_class='post')

        # find the total number of posts
        total_posts = options.count or int(soup.posts('total'))
        last_post = options.skip + total_posts

        def _backup(posts):
            for p in sorted(posts, key=lambda x: long(x('id')), reverse=True):
                post = post_class(p)
                if ident_max and long(post.ident) <= ident_max:
                    return False
                if options.period:
                    if post.date >= p_stop:
                        continue
                    if post.date < p_start:
                        return False
                post.generate_content()
                if post.error:
                    sys.stderr.write('%s%s\n' % (post.error, 50 * ' '))
                post.save_post()
                self.post_count += 1
            return True

        # Get the XML entries from the API, which we can only do for max 50 posts at once.
        # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
        MAX = 50
        for i in range(options.skip, last_post, MAX):
            # find the upper bound
            j = min(i + MAX, last_post)
            log(account, "Getting posts %d to %d of %d\r" % (i, j - 1, total_posts))

            soup = xmlparse('%s?num=%d&start=%d' % (base, j - i, i))
            if soup is None:
                return

            if not _backup(soup.posts['post':]):
                break

        if not options.blosxom and self.post_count:
            get_avatar()
            get_style()
            if not have_custom_css:
                save_style()
            self.index = defaultdict(lambda: defaultdict(list))
            self.build_index()
            self.save_index()

        log(account, "%d posts backed up\n" % self.post_count)
        self.total_count += self.post_count


class TumblrPost:

    def __init__(self, post):
        self.content = ''
        self.post = post
        self.xml_content = post.__repr__(1, 1)
        self.ident = post('id')
        self.url = post('url')
        self.typ = post('type')
        self.date = int(post('unix-timestamp'))
        self.tm = time.localtime(self.date)
        self.title = ''
        self.tags = []
        self.file_name = self.ident + post_ext
        self.error = None

    def generate_content(self):
        """generates the content for this post"""
        post = self.post
        content = []

        def append(s, fmt=u'%s'):
            # the %s conversion calls unicode() on the xmltramp element
            content.append(fmt % s)

        def get_try(elt):
            try:
                return unicode(post[elt])
            except KeyError:
                return ''

        def append_try(elt, fmt=u'%s'):
            elt = get_try(elt)
            if elt:
                append(elt, fmt)

        if self.typ == 'regular':
            self.title = get_try('regular-title')
            append_try('regular-body')

        elif self.typ == 'photo':
            url = escape(get_try('photo-link-url'))
            for p in post.photoset['photo':] if hasattr(post, 'photoset') else [post]:
                src = unicode(p['photo-url'])
                append(escape(self.get_image_url(src)), u'<img alt="" src="%s">')
                if url:
                    content[-1] = '<a href="%s">%s</a>' % (url, content[-1])
                content[-1] = '<p>' + content[-1] + '</p>'
                if p._name == 'photo' and p('caption'):
                    append(p('caption'), u'<p>%s</p>')
            append_try('photo-caption')

        elif self.typ == 'link':
            url = unicode(post['link-url'])
            self.title = u'<a href="%s">%s</a>' % (escape(url),
                post['link-text'] if 'link-text' in post else url
            )
            append_try('link-description')

        elif self.typ == 'quote':
            append(post['quote-text'], u'<blockquote><p>%s</p></blockquote>')
            append_try('quote-source', u'<p>%s</p>')

        elif self.typ == 'video':
            source = unicode(post['video-source']).strip()
            if source.startswith('<iframe') or source.startswith('<object'):
                append(source, u'<p>%s</p>')
                append_try('video-caption')
            else:
                append(post['video-player'], u'<p>%s</p>')
                append_try('video-caption')
                append(escape(source), u'<p><a href="%s">Original</a></p>')

        elif self.typ == 'audio':
            append(post['audio-player'])
            append_try('audio-caption')

        elif self.typ == 'answer':
            self.title = post.question
            append(post.answer)

        elif self.typ == 'conversation':
            self.title = get_try('conversation-title')
            append(
                '<br>\n'.join(escape(unicode(l)) for l in post.conversation['line':]),
                u'<p>%s</p>'
            )

        else:
            self.error = u"Unknown post type '%s' in post #%s" % (self.typ, self.ident)
            append(escape(self.xml_content), u'<pre>%s</pre>')

        self.tags = [u'%s' % t for t in post['tag':]]

        self.content = '\n'.join(content)

        # fix wrongly nested HTML tags
        for p in ('<p>(<(%s)>)', '(</(%s)>)</p>'):
            self.content = re.sub(p % 'p|ol|iframe[^>]*', r'\1', self.content)

    def get_image_url(self, url):
        return save_image(url)

    def get_post(self):
        """returns this post in HTML"""
        post = post_header + '<article class=%s id=p-%s>\n' % (self.typ, self.ident)
        post += '<p class=meta><span class=date>%s</span>\n' % strftime('%x %X', self.tm)
        post += u'<a class=llink href=../%s/%s>¶</a>\n' % (post_dir, self.file_name)
        post += u'<a href=%s rel=canonical>●</a></p>\n' % self.url
        if self.title:
            post += '<h2>%s</h2>\n' % self.title
        post += self.content
        if self.tags:
            post += u'\n<p class=tags>%s</p>' % u' '.join(u'#' + t for t in self.tags)
        post += '\n</article>\n'
        return post

    def save_post(self):
        """saves this post locally"""
        with open_text(post_dir, self.file_name) as f:
            f.write(self.get_post())
        os.utime(path_to(post_dir, self.file_name),
            (self.date, self.date)
        )
        if options.xml:
            with open_text(xml_dir, self.ident + '.xml') as f:
                f.write(self.xml_content)

class BlosxomPost(TumblrPost):

    def get_image_url(self, url):
        return url

    def get_post(self):
        """returns this post as a Blosxom post"""
        post = self.title + '\nmeta-id: _' + self.ident + '\nmeta-url: ' + self.url
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
        self.file_name = os.path.split(post_file)[1]
        self.ident = os.path.splitext(self.file_name)[0]
        self.date = os.stat(post_file).st_mtime
        self.tm = time.localtime(self.date)

    def get_post(self):
        return u''.join(self.lines)


if __name__ == '__main__':
    import optparse
    parser = optparse.OptionParser("Usage: %prog [options] blog-name ...",
        description="Makes a local backup of Tumblr blogs."
    )
    parser.add_option('-q', '--quiet', action='store_true',
        help="suppress progress messages"
    )
    parser.add_option('-i', '--incremental', action='store_true',
        help="incremental backup mode"
    )
    parser.add_option('-x', '--xml', action='store_true',
        help="save the original XML source"
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
    parser.add_option('-n', '--count', type='int', help="save only COUNT posts")
    parser.add_option('-s', '--skip', type='int', default=0,
        help="skip the first SKIP posts"
    )
    parser.add_option('-p', '--period', help="limit the backup to PERIOD"
        " ('y', 'm', 'd' or YYYY[MM[DD]])"
    )
    parser.add_option('-P', '--private', help="password for a private tumblr",
        metavar='PASSWORD'
    )
    options, args = parser.parse_args()

    if options.auto is not None:
        if options.auto == time.localtime().tm_hour:
            options.incremental = False
        else:
            options.incremental = True
    if options.period:
        try:
            options.period = time.strftime(
                {'y': '%Y', 'm': '%Y%m', 'd': '%Y%m%d'}[options.period]
            )
        except KeyError:
            options.period = options.period.replace('-', '')
        if len(options.period) not in (4, 6, 8):
            parser.error("Period must be 'y', 'm', 'd' or YYYY[MM[DD]]")
    if not args:
        args = ['bbolli']

    tb = TumblrBackup()
    for account in args:
        tb.backup(account)

    sys.exit(0 if tb.total_count else 1)
