#!/usr/bin/python -u
# encoding: utf-8

# standard Python library imports
from __future__ import with_statement
import os
import sys
import urllib
import urllib2
import pprint
from xml.sax.saxutils import escape
import codecs
import imghdr
from collections import defaultdict
import time
import netrc

# extra required packages
import xmltramp

verbose = True
root_folder = os.getcwdu()
count = None            # None = all posts
start = 0               # 0 = most recent post
period = None           # YYYY[MM[DD]] to be backed up
theme = False
account = 'bbolli'

# add another JPEG recognizer
# see http://www.garykessler.net/library/file_sigs.html
def test_jpg(h, f):
    if h[:3] == '\xFF\xD8\xFF' and h[3] in "\xDB\xE0\xE1\xE2\xE3":
        return 'jpg'

imghdr.tests.append(test_jpg)

# directory names, will be set in TumblrBackup.backup()
save_folder = ''
post_dir = 'posts'
image_dir = 'images'
image_folder = ''
archive_dir = 'archive'
theme_dir = 'theme'

# HTML fragments
post_header = ''
footer = u'</body>\n</html>\n'

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
        mkdir(os.path.join(save_folder, *parts[:-1]))
    return codecs.open(os.path.join(save_folder, *parts), 'w', 'utf-8')

def save_image(image_url):
    """saves an image if not saved yet"""
    image_filename = image_url.split('/')[-1]
    if '.' not in image_filename:
        # read just the first 32 bytes of the image
        header_req = urllib2.Request(image_url)
        header_req.headers['Range'] = 'bytes=0-31'
        header_resp = urllib2.urlopen(header_req)
        image_header = header_resp.read()
        image_type = imghdr.what(None, image_header)
        if image_type:
            if image_type == 'jpeg':
                image_type = 'jpg'
            image_filename += '.' + image_type
        header_resp.close()
    mkdir(image_folder)
    local_image_path = os.path.join(image_folder, image_filename)
    if not os.path.exists(local_image_path):
        # only download images if they don't already exist
        image_response = urllib2.urlopen(image_url)
        with open(local_image_path, 'wb') as image_file:
            image_file.write(image_response.read())
        image_response.close()
    return image_filename

def header(heading, title='', body_class='', subtitle='', avatar=''):
    if body_class:
        body_class = ' class=' + body_class
        style = '''
<style>
.archive h1, .subtitle, article {
    padding-bottom: 0.75em; border-bottom: 1px #ccc dotted; margin-bottom: 0.75em;
}
</style>
'''
    else:
        style = ''
    h = u'''<!DOCTYPE html>
<html>
<head><meta charset=utf-8><title>%s</title>%s</head>
<body%s>

''' % (heading, style, body_class)
    if avatar:
        h += '<img src=%s/%s alt=Avatar style="float: right;">\n' % (theme_dir, avatar)
    if title:
        h += '<h1>%s</h1>\n' % title
    if subtitle:
        h += '<p class=subtitle>%s</p>\n' % subtitle
    return h


class TumblrBackup:

    def save_index(self):
        with open_text('index.html') as idx:
            idx.write(header(self.title, self.title, body_class='index',
                subtitle=self.subtitle, avatar=self.avatar
            ))
            for year in sorted(self.index.keys(), reverse=True):
                self.save_year(idx, year)
            idx.write(footer)

    def save_year(self, idx, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=True):
            tm = time.localtime(time.mktime([year, month, 3, 0, 0, 0, 0, 0, -1]))
            month_name = self.save_month(year, month, tm)
            idx.write('    <li><a href=%s/%s>%s</a></li>\n' % (
                archive_dir, month_name, time.strftime('%B', tm)
            ))
        idx.write('</ul>\n')

    def save_month(self, year, month, tm):
        file_name = '%d-%02d.html' % (year, month)
        with open_text(archive_dir, file_name) as arch:
            arch.write('\n\n'.join([
                header(self.title, time.strftime('%B %Y', tm), body_class='archive'),
                '\n\n'.join(p.get_post(True) for p in self.index[year][month]),
                '<p><a href=../>Index</a></p>',
                footer
            ]))
        return file_name

    def get_theme(self, host, user, password):
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
            if attrs.get('is-primary') != 'yes':
                continue
            if hasattr(log, 'custom-css'):
                with open_text(theme_dir, 'custom.css') as f:
                    f.write(log['custom-css'][0])
            if hasattr(log, 'theme-source'):
                with open_text(theme_dir, 'theme.html') as f:
                    f.write(log['theme-source'][0])
            avatar_url = attrs.get('avatar-url')
            if avatar_url:
                avatar = urllib2.urlopen(avatar_url)
                avatar_file = 'avatar.' + avatar_url.split('.')[-1]
                with open(os.path.join(save_folder, theme_dir, avatar_file), 'wb') as f:
                    f.write(avatar.read())
                    self.avatar = avatar_file

    def backup(self, account):
        """makes HTML files and an index for every post on a public Tumblr blog account"""

        # construct the tumblr API URL
        base = 'http://' + account
        if '.' not in account:
            base += '.tumblr.com'
        base += '/api/read'

        # make sure there are folders to save in
        global save_folder, image_folder
        save_folder = os.path.join(root_folder, account)
        mkdir(save_folder, True)
        image_folder = os.path.join(save_folder, image_dir)

        self.index = defaultdict(lambda: defaultdict(list))
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
                self.get_theme(host, auth[0], auth[2])

        # start by calling the API with just a single post
        log("Getting basic information\r")
        try:
            response = urllib2.urlopen(base + '?num=1')
        except urllib2.URLError:
            sys.stderr.write("Invalid URL %s\n" % base)
            sys.exit(2)
        soup = xmltramp.parse(response.read())

        # collect all the meta information
        tumblelog = soup.tumblelog
        try:
            self.title = escape(tumblelog('title'))
        except KeyError:
            self.title = account
        self.subtitle = unicode(tumblelog)

        # use it to create a header
        global post_header
        post_header = header(self.title)

        # find the total number of posts
        total_posts = count or int(soup.posts('total'))

        # Get the XML entries from the API, which we can only do for max 50 posts at once.
        # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
        max = 50
        n_posts = 0
        for i in range(start, start + total_posts, max):
            # find the upper bound
            j = i + max
            if j > start + total_posts:
                j = start + total_posts
            log("Getting posts %d to %d of %d...\r" % (i, j - 1, total_posts))

            response = urllib2.urlopen('%s?num=%d&start=%d' % (base, j - i, i))
            soup = xmltramp.parse(response.read())

            for p in soup.posts['post':]:
                post = TumblrPost(p)
                if period:
                    if post.date >= p_stop:
                        continue
                    if post.date < p_start:
                        i = None
                        break
                if post.error:
                    sys.stderr.write('%r in post #%s%s\n' % (post.error, post.ident, 50 * ' '))
                if post.save_post():
                    self.index[post.tm.tm_year][post.tm.tm_mon].append(post)
                    n_posts += 1

            if i is None:
                break

        if self.index:
            self.save_index()

        log("%d posts backed up" % n_posts + 50 * ' ' + '\n')


class TumblrPost:

    def __init__(self, post):
        self.content = ''
        self.ident = post('id')
        self.error = None
        try:
            self.typ = post('type')
            self.date = int(post('unix-timestamp'))
            self.tm = time.localtime(self.date)
            self.file_name = self.ident + '.html'
            self.generate_content(post)
        except Exception, e:
            self.error = e
            self.content = u'<p class=error>%r</p>\n<pre>%s</pre>' % (e, pprint.pformat(post()))

    def generate_content(self, post):
        """generates HTML source for this post"""
        content = []
        append = content.append

        if self.typ == 'regular':
            try:
                append('<h2>' + unicode(post['regular-title']) + '</h2>')
            except KeyError:
                pass
            try:
                append(unicode(post['regular-body']))
            except KeyError:
                pass

        elif self.typ == 'photo':
            try:
                append(unicode(post['photo-caption']))
            except KeyError:
                pass
            append(u'<img alt="" src="../%s/%s">' % (image_dir, save_image(unicode(post['photo-url']))))

        elif self.typ == 'link':
            text = post['link-text']
            url = post['link-url']
            append(u'<h2><a href="%s">%s</a></h2>' % (url, text))
            try:
                append(unicode(post['link-description']))
            except KeyError:
                pass

        elif self.typ == 'quote':
            quote = unicode(post['quote-text'])
            source = unicode(post['quote-source'])
            append(u'<blockquote>%s</blockquote>\n<p>%s</p>' % (quote, source))

        elif self.typ == 'video':
            try:
                caption = unicode(post['video-caption'])
            except:
                caption = ''
            source = unicode(post['video-source'])
            if source.startswith('<iframe'):
                append(u'%s\n%s' % (source, caption))
            else:
                player = unicode(post['video-player'])
                append(u'%s\n%s\n<p><a href="%s">Original</a></p>' % (player, caption, source))

        elif self.typ in ('answer',):
            return ''

        else:
            append(u'<pre>%s</pre>' % pprint.pformat(post()))

        tags = post['tag':]
        if tags:
            append(u'<p class=tags>%s</p>' % ' '.join('#' + unicode(t) for t in tags))

        self.content = '\n'.join(content)

    def get_post(self, link=False):
        """returns this post in HTML"""
        post = '<article class=%s id=p-%s>\n' % (self.typ, self.ident)
        post += '<p class=meta><span class=date>%s</span>' % time.strftime('%x %X', self.tm)
        if link:
            post += u'\n<span class=link><a href=../%s/%s>¶</a></span>' % (post_dir, self.file_name)
        post += '</p>\n' + self.content + '\n</article>'
        return post

    def save_post(self):
        """saves this post locally"""
        if not self.content:
            return False
        with open_text(post_dir, self.file_name) as f:
            f.write(post_header + self.get_post() + '\n\n' + footer)
        os.utime(os.path.join(save_folder, post_dir, self.file_name),
            (self.date, self.date)
        )
        return True


if __name__ == '__main__':
    import getopt
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'qn:s:p:t')
    except getopt.GetoptError:
        print "Usage: %s [-q] [-n post-count] [-s start-post] [-p y|m|d|YYYY[MM[DD]]] [-t] [userid]" % sys.argv[0]
        sys.exit(1)
    for o, v in opts:
        if o == '-q':
            verbose = False
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
        elif o == '-t':
            theme = True
    if args:
        account = args[0]
    try:
        TumblrBackup().backup(account)
    except Exception, e:
        sys.stderr.write('%r\n' % e)
        sys.exit(2)
