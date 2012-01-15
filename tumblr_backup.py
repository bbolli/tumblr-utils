#!/usr/bin/python -u
# encoding: utf-8

# standard Python library imports
import os
import sys
import urllib2
import pprint
from xml.sax.saxutils import escape
import codecs
import imghdr
from collections import defaultdict
import time

# extra required packages
import xmltramp

# Tumblr specific constants
TUMBLR_URL = '.tumblr.com/api/read'

verbose = True
root_folder = os.getcwdu()
count = None            # None = all posts
start = 0               # 0 = most recent post
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
post_folder = ''
image_dir = 'images'
image_folder = ''
archive_dir = 'archive'
archive_folder = ''

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

def save_image(image_url):
    """saves an image if not saved yet"""
    image_filename = image_url.split('/')[-1]
    if '.' not in image_filename:
        image_response = urllib2.urlopen(image_url)
        image_header = image_response.read(32)
        image_type = imghdr.what(None, image_header)
        if image_type:
            image_type = {'jpeg': 'jpg'}.get(image_type, image_type)
            image_filename += '.' + image_type
    else:
        image_response = None
        image_header = ''
    mkdir(image_folder)
    local_image_path = os.path.join(image_folder, image_filename)
    if not os.path.exists(local_image_path):
        # only download images if they don't already exist
        if not image_response:
            image_response = urllib2.urlopen(image_url)
        image_file = open(local_image_path, 'wb')
        image_file.write(image_header + image_response.read())
        image_file.close()
    if image_response:
        image_response.close()
    return image_filename

def header(heading, title='', body_class='', subtitle=''):
    if body_class:
        body_class = ' class=' + body_class
    h = u'''<!DOCTYPE html>
<html>
<head><meta charset=utf-8><title>%s</title></head>
<body%s>
''' % (heading, body_class)
    if title:
        h += '<h1>%s</h1>\n' % title
    if subtitle:
        h += '<p class=subtitle>%s</p>\n' % subtitle
    return h


class TumblrBackup:

    def save_index(self):
        idx = open(os.path.join(save_folder, 'index.html'), 'w')
        idx.write(header(self.title, self.title, body_class='index', subtitle=self.subtitle))
        for year in sorted(self.index.keys(), reverse=True):
            self.save_year(idx, year)
        idx.write(footer)
        idx.close()

    def save_year(self, idx, year):
        idx.write('<h3>%s</h3>\n<ul>\n' % year)
        for month in sorted(self.index[year].keys(), reverse=True):
            tm = time.localtime(time.mktime([year, month, 3, 0, 0, 0, 0, 0, -1]))
            month_name = self.save_month(year, month, tm)
            idx.write('<li><a href=%s/%s>%s</a></li>\n' % (
                archive_dir, month_name, time.strftime('%B', tm)
            ))
        idx.write('</ul>\n')

    def save_month(self, year, month, tm):
        file_name = '%d-%02d.html' % (year, month)
        mkdir(archive_folder)
        arch = open(os.path.join(archive_folder, file_name), 'w')
        arch.write('\n\n'.join([
            header(self.title, time.strftime('%B %Y', tm), body_class='archive'),
            '\n\n'.join(p.meta(True) + p.content for p in self.index[year][month]),
            footer
        ]))
        arch.close()
        return file_name

    def backup(self, account):
        """makes HTML files and an index for every post on a public Tumblr blog account"""

        log("Getting basic information\r")
        base = 'http://' + account + TUMBLR_URL

        # make sure there are folders to save in
        global save_folder, post_folder, image_folder, archive_folder
        save_folder = os.path.join(root_folder, account)
        mkdir(save_folder, True)
        post_folder = os.path.join(save_folder, post_dir)
        image_folder = os.path.join(save_folder, image_dir)
        archive_folder = os.path.join(save_folder, archive_dir)

        self.index = defaultdict(lambda: defaultdict(list))

        # start by calling the API with just a single post
        try:
            response = urllib2.urlopen(base + '?num=1')
        except urllib2.URLError:
            sys.stderr.write("Invalid URL %s\n" % base)
            sys.exit(2)
        soup = xmltramp.parse(response.read())

        # collect all the meta information
        tumblelog = soup.tumblelog
        self.title = escape(tumblelog('title'))
        self.subtitle = escape(unicode(tumblelog))

        # use it to create a header
        global post_header
        post_header = header(self.title)

        # find the total number of posts
        total_posts = count or int(soup.posts('total'))

        # Get the XML entries from the API, which we can only do for max 50 posts at once.
        # Posts "arrive" in reverse chronological order. Post #0 is the most recent one.
        max = 50
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
                if hasattr(post, 'error'):
                    sys.stderr.write('%r in post #%s%s\n' % (post.error, post.ident, 50 * ' '))
                elif post.save_post():
                    self.index[post.tm.tm_year][post.tm.tm_mon].append(post)

        if self.index:
            self.save_index()

        log("Backup complete" + 50 * ' ' + '\n')


class TumblrPost:

    def __init__(self, post):
        self.content = ''
        self.ident = post('id')
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

    def meta(self, link=False):
        """returns this post's meta data in HTML"""
        meta = u'<!-- type: %s, id: %s -->\n<p class=meta><span class=date>%s</span>' % (
            self.typ, self.ident, time.strftime('%x %X', self.tm)
        )
        if link:
            meta += u'\n<span class=link><a href=../%s/%s>¶</a></span>' % (post_dir, self.file_name)
        return meta + '</p>\n'

    def save_post(self):
        """saves this post locally"""
        if not self.content:
            return False
        mkdir(post_folder)
        file_name = os.path.join(post_folder, self.file_name)
        f = codecs.open(file_name, 'w', 'utf-8')
        try:
            f.write(post_header + self.meta() + self.content + footer)
        except:
            return False
        finally:
            f.close()
        os.utime(file_name, (self.date, self.date))
        return True


if __name__ == '__main__':
    import getopt
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'qn:s:')
    except getopt.GetoptError:
        print "Usage: %s [-q] [-n post-count] [-s start-post] [userid]" % sys.argv[0]
        sys.exit(1)
    for o, v in opts:
        if o == '-q':
            verbose = False
        elif o == '-n':
            count = int(v)
        elif o == '-s':
            start = int(v)
    if args:
        account = args[0]
    try:
        TumblrBackup().backup(account)
    except Exception, e:
        sys.stderr.write('%r\n' % e)
        sys.exit(2)
