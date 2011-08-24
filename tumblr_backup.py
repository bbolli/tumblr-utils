#!/usr/bin/python -u

# standard Python library imports
import os
import sys
import urllib2
import pprint
from xml.sax.saxutils import escape
import codecs

# extra required packages
import xmltramp

# Tumblr specific constants
TUMBLR_URL = '.tumblr.com/api/read'

verbose = True
n_last = None           # None = all posts


def log(s):
    if verbose:
        print s,

def savePost(post, header, save_folder):
    """saves an individual post and any resources for it locally"""

    slug = post('id')
    date_gmt = post('date')
    date_unix = int(post('unix-timestamp'))
    type = post('type')

    file_name = os.path.join(save_folder, slug + '.html')
    f = codecs.open(file_name, 'w', 'utf-8')

    # header info which is the same for all posts
    f.write(u'%s<p class=date>%s</p>\n' % (header, date_gmt))

    if type == 'regular':
        try:
            f.write('<h2>' + unicode(post['regular-title']) + '</h2>\n')
        except KeyError:
            pass
        try:
            f.write(unicode(post['regular-body']) + '\n')
        except KeyError:
            pass

    elif type == 'photo':
        try:
            caption = unicode(post['photo-caption']) + '\n'
        except KeyError:
            caption = u''
        image_url = unicode(post['photo-url'])

        image_filename = image_url.split('/')[-1]
        image_folder = os.path.join(save_folder, 'images')
        if not os.path.exists(image_folder):
            os.mkdir(image_folder)
        local_image_path = os.path.join(image_folder, image_filename)

        if not os.path.exists(local_image_path):
            # only download images if they don't already exist
            image_response = urllib2.urlopen(image_url)
            image_file = open(local_image_path, 'wb')
            image_file.write(image_response.read())
            image_file.close()

        f.write(caption + u'<img alt="%s" src="images/%s">\n' % (caption, image_filename))

    elif type == 'link':
        text = post['link-text']
        url = post['link-url']
        f.write(u'<h2><a href="%s">%s</a></h2>\n' % (url, text))
        try:
            f.write(unicode(post['link-description']) + '\n')
        except KeyError:
            pass

    elif type == 'quote':
        quote = unicode(post['quote-text'])
        source = unicode(post['quote-source'])
        f.write(u'<blockquote>%s</blockquote>\n<p>%s</p>\n' % (quote, source))

    elif type == 'video':
        caption = unicode(post['video-caption'])
        source = unicode(post['video-source'])
        player = unicode(post['video-player'])
        f.write(player + u'\n<a href="%s">%s</a>\n' % (source, caption))

    else:
        f.write(u'<!-- type: %s -->\n<pre>%s</pre>\n' % (type, pprint.pformat(post())))

    # common footer
    tags = post['tag':]
    if tags:
        f.write(u'<p class=tags>%s</p>\n' % ' '.join('#' + unicode(t) for t in tags))
    f.write('</body>\n</html>\n')

    f.close()
    os.utime(file_name, (date_unix, date_unix))

def backup(account):
    """makes HTML files for every post on a public Tumblr blog account"""

    log("Getting basic information\r")
    base = 'http://' + account + TUMBLR_URL

    # make sure there's a folder to save in
    save_folder = os.path.join(os.getcwd(), account)
    if not os.path.exists(save_folder):
        os.mkdir(save_folder)

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
    header = u'''<!DOCTYPE html>
<html>
<head><title>%s</title></head>
<body>
<h1>%s</h1>
''' % (title, title)
    if subtitle:
        header += u'<p class=subtitle>%s</p>\n' % subtitle

    # then find the total number of posts
    total_posts = n_last or int(soup.posts('total'))

    # then get the XML entries from the API, which we can only do for max 50 posts at once
    max = 50
    for i in range(0, total_posts, max):
        # find the upper bound
        j = i + max
        if j > total_posts:
            j = total_posts
        log("Getting posts %d to %d of %d...\r" % (i, j - 1, total_posts))

        response = urllib2.urlopen(base + '?num=%d&start=%d' % (j - i, i))
        soup = xmltramp.parse(response.read())

        for post in soup.posts['post':]:
            savePost(post, header, save_folder)

    log("Backup complete" + 50 * ' ' + '\n')

if __name__ == '__main__':
    import getopt
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'qn:')
        if len(args) != 1:
            raise getopt.GetoptError("")
    except getopt.GetoptError:
        print "Usage: %s [-q] [-n last-posts] userid" % sys.argv[0]
        sys.exit(1)
    for o, v in opts:
        if o == '-q':
            verbose = False
        elif o == '-n':
            n_last = int(v)
    try:
        backup(args[0])
    except Exception, e:
        sys.stderr.write('%r\n' % e)
        sys.exit(2)
