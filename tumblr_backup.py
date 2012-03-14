#!/usr/bin/python -u
# encoding: utf-8

# standard Python library imports
from __future__ import with_statement
import os
import sys
import urllib
import urllib2
from xml.sax.saxutils import escape
import codecs
import imghdr
from collections import defaultdict
import time
import netrc
import locale
import subprocess
from glob import glob

# extra required packages
import xmltramp

join = os.path.join

verbose = True
incremental = False
xml = False
root_folder = os.getcwdu()
count = None            # None = all posts
start = 0               # 0 = most recent post
period = None           # YYYY[MM[DD]] to be backed up
theme = False
blosxom = False
reverse_archive = True
reverse_index = True

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
post_dir = 'posts'
xml_dir = 'xml'
image_dir = 'images'
archive_dir = 'archive'
theme_dir = 'theme'
backup_css = '_local.css'

# HTML fragments
post_header = ''
footer = u'</body>\n</html>\n'

post_ext = '.html'

# ensure the right date/time format
try:
    locale.setlocale(locale.LC_TIME, '')
except locale.Error:
    pass

def log(s):
    if verbose:
        print s,

def mkdir(dir, recursive=False):
    if not os.path.exists(dir):
        if recursive:
            os.makedirs(dir)
        else:
            os.mkdir(dir)

def open_text(*parts):
    if len(parts) > 1:
        mkdir(join(save_folder, *parts[:-1]))
    return codecs.open(join(save_folder, *parts), 'w', 'utf-8')

def save_image(image_url):
    """saves an image if not saved yet"""
    image_filename = image_url.split('/')[-1]
    glob_filter = '' if '.' in image_filename else '.*'
    # check if a file with this name already exists
    image_glob = glob(join(image_folder, image_filename + glob_filter))
    if image_glob:
        return os.path.split(image_glob[0])[1]
    # download the image data
    image_response = urllib2.urlopen(image_url)
    image_data = image_response.read()
    image_response.close()
    # determine the file type if it's unknown
    if '.' not in image_filename:
        image_type = imghdr.what(None, image_data[:32])
        if image_type:
            if image_type == 'jpeg':
                image_type = 'jpg'
            image_filename += '.' + image_type
    # save the image
    mkdir(image_folder)
    with open(join(image_folder, image_filename), 'wb') as image_file:
        image_file.write(image_data)
    return image_filename

def header(heading, title='', body_class='', subtitle='', avatar=''):
    theme_rel = '../' + theme_dir
    if body_class:
        if body_class == 'index':
            theme_rel = theme_dir
        body_class = ' class=' + body_class
    h = u'''<!DOCTYPE html>
<html>
<head><meta charset=utf-8><title>%s</title>
<link rel=stylesheet type=text/css href=%s/%s>
</head>

<body%s>

''' % (heading, theme_rel, backup_css, body_class)
    if avatar:
        h += '<img src=%s/%s alt=Avatar style="float: right;">\n' % (theme_rel, avatar)
    if title:
        h += u'<h1>%s</h1>\n' % title
    if subtitle:
        h += u'<p class=subtitle>%s</p>\n' % subtitle
    return h


class TumblrBackup:

    def save_style(self):
        with open_text(theme_dir, backup_css) as css:
            css.write('''
body {
    width: 720px; margin: 0 auto;
}
img {
    max-width: 720px;
}
.archive h1, .subtitle, article {
    padding-bottom: 0.75em; border-bottom: 1px #ccc dotted; margin-bottom: 0.75em;
}
.meta a {
    text-decoration: none;
}
.post a.llink, .archive a.tlink {
    display: none;
}
blockquote {
    margin-left: 0; border-left: 8px #999 solid; padding: 0 24px;
}
''')

    def build_index(self):
        for f in glob(join(save_folder, post_dir, '*.html')):
            post = LocalPost(f)
            self.index[post.tm.tm_year][post.tm.tm_mon].append(post)

    def save_index(self):
        with open_text('index.html') as idx:
            idx.write(header(self.title, self.title, body_class='index',
                subtitle=self.subtitle, avatar=self.avatar
            ))
            for year in sorted(self.index.keys(), reverse=reverse_index):
                self.save_year(idx, year)
            idx.write(footer)

    def save_year(self, idx, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=reverse_index):
            tm = time.localtime(time.mktime([year, month, 3, 0, 0, 0, 0, 0, -1]))
            month_name = self.save_month(year, month, tm)
            idx.write('    <li><a href=%s/%s>%s</a></li>\n' % (
                archive_dir, month_name, time.strftime('%B', tm).decode('utf-8')
            ))
        idx.write('</ul>\n')

    def save_month(self, year, month, tm):
        file_name = '%d-%02d.html' % (year, month)
        with open_text(archive_dir, file_name) as arch:
            arch.write('\n\n'.join([
                header(self.title, time.strftime('%B %Y', tm).decode('utf-8'), body_class='archive'),
                '\n\n'.join(p.get_post() for p in sorted(
                    self.index[year][month], key=lambda x: x.date, reverse=reverse_archive
                )),
                '<p><a href=../>Index</a></p>',
                footer
            ]))
        return file_name

    def get_theme(self, account, host, user, password):
        subprocess.call(['/bin/rm', '-rf', join(save_folder, theme_dir)])
        try:
            info = urllib2.urlopen('http://%s/api/authenticate' % host,
                urllib.urlencode({
                    'email': user, 'password': password, 'include-theme': '1'
                })
            )
        except urllib2.URLError:
            return
        tumblr = xmltramp.parse(info.read())
        if tumblr._name != 'tumblr':
            return
        for log in tumblr['tumblelog':]:
            attrs = log()
            if attrs.get('name') != account:
                continue
            if hasattr(log, 'custom-css') and len(log['custom-css']):
                with open_text(theme_dir, 'custom.css') as f:
                    f.write(log['custom-css'][0])
            if hasattr(log, 'theme-source') and len(log['theme-source']):
                with open_text(theme_dir, 'theme.html') as f:
                    f.write(log['theme-source'][0])
            avatar_url = attrs.get('avatar-url')
            if avatar_url:
                mkdir(join(save_folder, theme_dir))
                avatar = urllib2.urlopen(avatar_url)
                avatar_file = 'avatar.' + avatar_url.split('.')[-1]
                with open(join(save_folder, theme_dir, avatar_file), 'wb') as f:
                    f.write(avatar.read())
                    self.avatar = avatar_file

    def backup(self, account):
        """makes single files and an index for every post on a public Tumblr blog account"""

        # construct the tumblr API URL
        base = 'http://' + account
        if '.' not in account:
            base += '.tumblr.com'
        base += '/api/read'

        # make sure there are folders to save in
        global save_folder, image_folder
        if blosxom:
            save_folder = root_folder
        else:
            save_folder = join(root_folder, account)
            image_folder = join(save_folder, image_dir)
        mkdir(save_folder, True)

        self.post_count = 0
        self.avatar = None

        # prepare the period start and end timestamps
        if period:
            i = 0; tm = [int(period[:4]), 1, 1, 0, 0, 0, 0, 0, -1]
            if len(period) >= 6:
                i = 1; tm[1] = int(period[4:6])
            if len(period) == 8:
                i = 2; tm[2] = int(period[6:8])
            p_start = time.mktime(tm)
            tm[i] += 1
            p_stop = time.mktime(tm)

        if theme:
            # if .netrc contains the login, get the style info
            host = 'www.tumblr.com'
            auth = netrc.netrc().authenticators(host)
            if auth:
                log("Getting the theme\r")
                self.get_theme(account, host, auth[0], auth[2])

        # get the highest post id already saved
        ident_max = None
        if incremental:
            try:
                ident_max = max(
                    long(os.path.splitext(os.path.split(f)[1])[0])
                    for f in glob(join(save_folder, post_dir, '*' + post_ext))
                )
                log('Backing up posts after %d\n' % ident_max)
            except ValueError:  # max() arg is an empty sequence
                pass

        # start by calling the API with just a single post
        log("Getting basic information\r")
        try:
            response = urllib2.urlopen(base + '?num=1')
        except urllib2.URLError:
            sys.stderr.write("Invalid URL %s\n" % base)
            return
        soup = xmltramp.parse(response.read())

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
        total_posts = count or int(soup.posts('total'))

        # Get the XML entries from the API, which we can only do for max 50 posts at once.
        # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
        MAX = 50
        for i in range(start, start + total_posts, MAX):
            # find the upper bound
            j = i + MAX
            if j > start + total_posts:
                j = start + total_posts
            log("Getting posts %d to %d of %d...\r" % (i, j - 1, total_posts))

            response = urllib2.urlopen('%s?num=%d&start=%d' % (base, j - i, i))
            soup = xmltramp.parse(response.read())

            for p in soup.posts['post':]:
                post = TumblrPost(p)
                if ident_max and long(post.ident) <= ident_max:
                    i = None
                    break
                if period:
                    if post.date >= p_stop:
                        continue
                    if post.date < p_start:
                        i = None
                        break
                if post.error:
                    sys.stderr.write('%r in post #%s%s\n' % (post.error, post.ident, 50 * ' '))
                post.save_post()
                self.post_count += 1

            if i is None:
                break

        if not blosxom and self.post_count:
            self.save_style()
            self.index = defaultdict(lambda: defaultdict(list))
            self.build_index()
            self.save_index()

        log("%d posts backed up" % self.post_count + 50 * ' ' + '\n')


class TumblrPost:

    def __init__(self, post):
        self.content = ''
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
        try:
            self.generate_content(post)
        except Exception, e:
            self.error = e
            self.content = u'<p class=error>%r</p>\n<pre>%s</pre>' % (e, escape(self.xml_content))

    def generate_content(self, post):
        """generates the content for this post"""
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
            if blosxom:
                append(post['photo-url'], u'<img alt="" src="%s">')
            else:
                append((image_dir, save_image(unicode(post['photo-url']))), u'<img alt="" src="../%s/%s">')
            url = get_try('photo-link-url')
            if url:
                content[0] = '<a href=%s>%s</a>' % (url, content[0])
            content[0] = '<p>' + content[0] + '</p>'
            append_try('photo-caption')

        elif self.typ == 'link':
            self.title = u'<a href="%s">%s</a>' % (post['link-url'], post['link-text'])
            append_try('link-description')

        elif self.typ == 'quote':
            append(post['quote-text'], u'<blockquote>%s</blockquote>')
            append_try('quote-source', u'<p>%s</p>')

        elif self.typ == 'video':
            source = unicode(post['video-source'])
            if source.startswith('<iframe') or source.startswith('<object'):
                append(source, u'<p>%s</p>')
                append_try('video-caption')
            else:
                append(post['video-player'], u'<p>%s</p>')
                append_try('video-caption')
                append(source, u'<p><a href="%s">Original</a></p>')

        elif self.typ == 'audio':
            append(post['audio-player'])
            append_try('audio-caption')

        elif self.typ == 'answer':
            self.title = post.question
            append(post.answer)

        else:
            raise ValueError('Unknown post type: ' + self.typ)

        self.tags = [u'%s' % t for t in post['tag':]]

        self.content = '\n'.join(content)

    def get_html(self):
        """returns this post in HTML"""
        post = '<article class=%s id=p-%s>\n' % (self.typ, self.ident)
        post += '<p class=meta><span class=date>%s</span>\n' % time.strftime('%x %X', self.tm)
        post += u'<a class=llink href=../%s/%s>¶</a>\n' % (post_dir, self.file_name)
        post += u'<a class=tlink href=%s>●</a></p>\n' % self.url
        if self.title:
            post += '<h2>%s</h2>\n' % self.title
        post += self.content
        if self.tags:
            post += u'\n<p class=tags>%s</p>' % u' '.join(u'#' + t for t in self.tags)
        post += '\n</article>'
        return post

    def get_blosxom(self):
        """returns this post as a Blosxom post"""
        post = self.title + '\nmeta-id: _' + self.ident + '\nmeta-url: ' + self.url
        if self.tags:
            post += '\nmeta-tags: ' + ' '.join(t.replace(' ', '+') for t in self.tags)
        post += '\n\n' + self.content
        return post

    def get_post(self):
        if blosxom:
            return self.get_blosxom()
        else:
            return post_header + self.get_html() + '\n\n' + footer

    def save_post(self):
        """saves this post locally"""
        with open_text(post_dir, self.file_name) as f:
            f.write(self.get_post())
        os.utime(join(save_folder, post_dir, self.file_name),
            (self.date, self.date)
        )
        if xml:
            with open_text(xml_dir, self.ident + '.xml') as f:
                f.write(self.xml_content)

class LocalPost(TumblrPost):

    def __init__(self, post_file):
        with codecs.open(post_file, 'r', 'utf-8') as f:
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
    import getopt
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'qixtbrRn:s:p:')
    except getopt.GetoptError:
        print "Usage: %s [-qixtbrR] [-n post-count] [-s start-post] [-p y|m|d|YYYY[MM[DD]]] [userid]..." % sys.argv[0]
        sys.exit(1)
    for o, v in opts:
        if o == '-q':
            verbose = False
        elif o == '-i':
            incremental = True
        elif o == '-x':
            xml = True
        elif o == '-t':
            theme = True
        elif o == '-b':
            blosxom = True
            post_ext = '.txt'
            post_dir = os.curdir
        elif o == '-r':
            reverse_archive = False
        elif o == '-R':
            reverse_index = False
        elif o == '-n':
            count = int(v)
        elif o == '-s':
            start = int(v)
        elif o == '-p':
            try:
                period = time.strftime(
                    {'y': '%Y', 'm': '%Y%m', 'd': '%Y%m%d'}[v]
                )
            except KeyError:
                period = v.replace('-', '')
            if len(period) not in (4, 6, 8):
                sys.stderr.write('Period must be y, m, d or YYYY[MM[DD]]\n')
                sys.exit(1)
    if not args:
        args = ['bbolli']
    tb = TumblrBackup()
    try:
        for account in args:
            tb.backup(account)
    except Exception, e:
        sys.stderr.write('%r\n' % e)
        sys.exit(2)
