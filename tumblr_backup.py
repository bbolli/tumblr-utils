#!/usr/bin/python -u

# standard Python library imports
import os
import sys
import urllib2
import pprint
from xml.sax.saxutils import escape

# extra required packages
import xmltramp

# Tumblr specific constants
TUMBLR_URL = ".tumblr.com/api/read"


def unescape(s):
    """ replace Tumblr's escaped characters with one's that make sense for saving in an HTML file """

    # special character corrections
    s = s.replace(u"\xa0", "&amp;nbsp;")
    s = s.replace(u"\xe1", "&amp;aacute;")

    # standard html
    s = s.replace("&lt;", "<")
    s = s.replace("&gt;", ">")
    s = s.replace("&amp;", "&") # this has to be last

    return s


def savePost(post, header, save_folder):
    """ saves an individual post and any resources for it locally """

    slug = post("id")
    date_gmt = post("date")
    date_unix = int(post("unix-timestamp"))
    type = post("type")

    file_name = os.path.join(save_folder, slug + ".html")
    f = open(file_name, "w")

    # header info which is the same for all posts
    f.write(
	header + "<!-- type: %s -->\n" % type +
	"<p>" + date_gmt + "</p>\n"
    )

    if type == "regular":
        try:
            f.write("<h2>" + str(post["regular-title"]) + "</h2>\n")
        except KeyError:
            pass
        try:
            f.write(str(post["regular-body"]) + "\n")
        except KeyError:
            pass

    elif type == "photo":
        try:
            caption = str(post["photo-caption"])
        except KeyError:
            caption = ''
        image_url = str(post["photo-url"])

        image_filename = image_url.split("/")[-1]
        image_folder = os.path.join(save_folder, "images")
        if not os.path.exists(image_folder):
            os.mkdir(image_folder)
        local_image_path = os.path.join(image_folder, image_filename)

        if not os.path.exists(local_image_path):
            # only download images if they don't already exist
            image_response = urllib2.urlopen(image_url)
            image_file = open(local_image_path, "wb")
            image_file.write(image_response.read())
            image_file.close()

        f.write(caption + "<img alt='" + caption + "' src='images/" + image_filename + "' />\n")

    elif type == "link":
        text = str(post["link-text"])
        url = str(post["link-url"])
        f.write("<p><a href='" + url + "'>" + text + "</a></p>\n")
        try:
            f.write(str(post["link-description"]) + "\n")
        except KeyError:
            pass

    elif type == "quote":
        quote = str(post["quote-text"])
        source = str(post["quote-source"])
        f.write("<blockquote>" + quote + "</blockquote>\n<p>" + source + "</p>\n")

    elif type == "video":
        caption = str(post["video-caption"])
        source = str(post["video-source"])
        player = str(post["video-player"])
        f.write(player + "\n<a href='" + source + "'>" + caption + "</a>\n")

    else:
        f.write("<pre>%s</pre>\n" % pprint.pformat(post()))

    # common footer
    f.write("</body>\n</html>\n")
    f.close()
    os.utime(file_name, (date_unix, date_unix))


def backup(account):
    """ makes HTML files for every post on a public Tumblr blog account """

    print "Getting basic information\r",
    base = "http://" + account + TUMBLR_URL

    # make sure there's a folder to save in
    save_folder = os.path.join(os.getcwd(), account)
    if not os.path.exists(save_folder):
        os.mkdir(save_folder)

    # start by calling the API with just a single post
    response = urllib2.urlopen(base + "?num=1")
    soup = xmltramp.parse(response.read())

    # collect all the meta information
    tumblelog = soup.tumblelog
    title = escape(tumblelog('title'))

    # use it to create a generic header for all posts
    header = "<!DOCTYPE html>\n<html><head><title>" + title + "</title></head><body>\n" + \
        "<h1>" + title + "</h1>\n<p>" + escape(str(tumblelog)) + "</p>\n"

    # then find the total number of posts
    total_posts = int(soup.posts("total"))

    # then get the XML files from the API, which we can only do with a max of 50 posts at once
    for i in range(0, total_posts, 50):
        # find the upper bound
        j = i + 49
        if j > total_posts:
            j = total_posts
        print "Getting posts %d to %d...  \r" % (i, j),

        response = urllib2.urlopen(base + "?num=50&start=%d" % i)
        soup = xmltramp.parse(response.read())

        for post in soup.posts["post":]:
            savePost(post, header, save_folder)

    print "Backup complete" + 50 * " "


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print "Invalid command line arguments. Please supply the name of your Tumblr account."
        sys.exit(1)
    try:
        backup(sys.argv[1])
    except Exception, e:
        sys.stderr.write("%r\n" % e)
        sys.exit(2)
