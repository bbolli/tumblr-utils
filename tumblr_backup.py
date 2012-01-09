#!/usr/bin/python -u

# standard Python library imports
import os
import sys
import urllib2
import pprint
from xml.sax.saxutils import escape
import codecs
import imghdr

# extra required packages
import xmltramp

# Tumblr specific constants
TUMBLR_URL = '.tumblr.com/api/read'

verbose = True
count = None            # None = all posts
start = 0               # 0 = most recent post
account = 'bbolli'

# add another JPEG recognizer
# see http://www.garykessler.net/library/file_sigs.html
def test_jpg(h, f):
    if h[:3] == '\xFF\xD8\xFF' and h[3] in "\xDB\xE0\xE1\xE2\xE3":
        return 'jpg'

imghdr.tests.append(test_jpg)


def log(s):
    if verbose:
        print s,

class TumblrBackup:

    def get_content(self, post):
        """generates HTML source for one post"""

        content = []
        append = content.append
        type = post('type')

        # header info which is the same for all posts
        append(u'<p class=date>%s</p>\n<!-- type: %s -->' % (post('date'), type))

        if type == 'regular':
            try:
                append('<h2>' + unicode(post['regular-title']) + '</h2>')
            except KeyError:
                pass
            try:
                append(unicode(post['regular-body']))
            except KeyError:
                pass

        elif type == 'photo':
            try:
                append(unicode(post['photo-caption']))
            except KeyError:
                pass
            append(u'<img alt="" src="%s/%s">' % (self.image_dir, self.save_image(unicode(post['photo-url']))))

        elif type == 'link':
            text = post['link-text']
            url = post['link-url']
            append(u'<h2><a href="%s">%s</a></h2>' % (url, text))
            try:
                append(unicode(post['link-description']))
            except KeyError:
                pass

        elif type == 'quote':
            quote = unicode(post['quote-text'])
            source = unicode(post['quote-source'])
            append(u'<blockquote>%s</blockquote>\n<p>%s</p>' % (quote, source))

        elif type == 'video':
            caption = unicode(post['video-caption'])
            source = unicode(post['video-source'])
            if source.startswith('<iframe'):
                append(u'%s\n%s' % (source, caption))
            else:
                player = unicode(post['video-player'])
                append(u'%s\n%s\n<p><a href="%s">Original</a></p>' % (player, caption, source))

        elif type in ('answer',):
            return ''

        else:
            append(u'<pre>%s</pre>' % pprint.pformat(post()))

        tags = post['tag':]
        if tags:
            append(u'<p class=tags>%s</p>' % ' '.join('#' + unicode(t) for t in tags))

        return '\n'.join(content)

    def save_post(self, post, content):
        """saves one post locally"""

        file_name = os.path.join(self.save_folder, post('id') + '.html')
        f = codecs.open(file_name, 'w', 'utf-8')
        try:
            f.write(self.header + content + self.footer)
        finally:
            f.close()
        self.set_date(file_name)

    def save_image(self, image_url):
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
        if not os.path.exists(self.image_folder):
            os.mkdir(self.image_folder)
        local_image_path = os.path.join(self.image_folder, image_filename)
        if not os.path.exists(local_image_path):
            # only download images if they don't already exist
            if not image_response:
                image_response = urllib2.urlopen(image_url)
            image_file = open(local_image_path, 'wb')
            image_file.write(image_header + image_response.read())
            image_file.close()
            self.set_date(local_image_path)
        if image_response:
            image_response.close()
        return image_filename

    def set_date(self, file_name):
        os.utime(file_name, (self.post_date, self.post_date))

    def backup(self, account):
        """makes HTML files for every post on a public Tumblr blog account"""

        log("Getting basic information\r")
        base = 'http://' + account + TUMBLR_URL

        # make sure there's a folder to save in
        self.save_folder = os.path.join(os.getcwd(), account)
        if not os.path.exists(self.save_folder):
            os.mkdir(self.save_folder)
        self.image_dir = 'images'
        self.image_folder = os.path.join(self.save_folder, self.image_dir)

        # start by calling the API with just a single post
        try:
            response = urllib2.urlopen(base + '?num=1')
        except urllib2.URLError:
            sys.stderr.write("Invalid URL %s\n" % base)
            sys.exit(2)
        soup = xmltramp.parse(response.read())

        # collect all the meta information
        tumblelog = soup.tumblelog
        title = escape(tumblelog('title'))
        subtitle = escape(unicode(tumblelog))

        # use it to create a generic header for all posts
        self.header = u'''<!DOCTYPE html>
<html>
<head><meta charset=utf-8><title>%s</title></head>
<body>
<h1>%s</h1>
''' % (title, title)
        if subtitle:
            self.header += u'<p class=subtitle>%s</p>\n' % subtitle

        self.footer = u'''
</body>
</html>
'''

        # then find the total number of posts
        total_posts = count or int(soup.posts('total'))

        # then get the XML entries from the API, which we can only do for max 50 posts at once
        max = 50
        for i in range(start, start + total_posts, max):
            # find the upper bound
            j = i + max
            if j > start + total_posts:
                j = start + total_posts
            log("Getting posts %d to %d of %d...\r" % (i, j - 1, total_posts))

            response = urllib2.urlopen('%s?num=%d&start=%d' % (base, j - i, i))
            soup = xmltramp.parse(response.read())

            for post in soup.posts['post':]:
                try:
                    self.post_date = int(post('unix-timestamp'))
                    content = self.get_content(post)
                    if content:
                        self.save_post(post, content)
                except Exception, e:
                    sys.stderr.write('%s%s\n' % (e, 50 * ' '))

        log("Backup complete" + 50 * ' ' + '\n')

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
    try:
        TumblrBackup().backup(args[0] if args else account)
    except Exception, e:
        sys.stderr.write('%r\n' % e)
        sys.exit(2)
