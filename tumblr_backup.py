
# standard Python library imports
import os
import sys
import urllib2

# extra required packages (StoneSoup is the version for XML)
from BeautifulSoup import BeautifulStoneSoup

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

    slug = post["url-with-slug"].rpartition("/")[2]
    date_gmt = post["date-gmt"]

    file_name = os.path.join(save_folder, slug + ".html")
    f = open(file_name, "w")

    # header info which is the same for all posts
    f.write(header)
    f.write("<p>" + date_gmt + "</p>")

    if post["type"] == "regular":
        title = post.find("regular-title").string
        body = post.find("regular-body").string

        f.write("<h2>" + title + "</h2>" + unescape(body))

    if post["type"] == "photo":
        caption = post.find("photo-caption").string
        image_url = post.find("photo-url", {"max-width": "1280"}).string

        image_filename = image_url.rpartition("/")[2] + ".jpg" # the 1280 size doesn't end with an extension strangely
        image_folder = os.path.join(save_folder, "images")
        if not os.path.exists(image_folder):
            os.mkdir(image_folder)
        local_image_path = os.path.join(image_folder, image_filename)

        if not os.path.exists(local_image_path):
            # only download images if they don't already exist
            print "Downloading a photo. This may take a moment."
            image_response = urllib2.urlopen(image_url)
            image_file = open(local_image_path, "wb")
            image_file.write(image_response.read())
            image_file.close()

        f.write(unescape(caption) + '<img alt="' + unescape(caption) + '" src="images/' + image_filename + '" />')

    if post["type"] == "quote":
        quote = post.find("quote-text").string
        source = post.find("quote-source").string

        f.write("<blockquote>" + unescape(quote) + "</blockquote><p>" + unescape(source) + "</p>")

    # common footer
    f.write("</body></html>")
    f.close()


def backup(account):
    """ makes HTML files for every post on a public Tumblr blog account """

    print "Getting basic information."

    # make sure there's a folder to save in
    save_folder = os.path.join(os.getcwd(), account)
    if not os.path.exists(save_folder):
        os.mkdir(save_folder)

    # start by calling the API with just a single post
    url = "http://" + account + TUMBLR_URL + "?num=1"
    response = urllib2.urlopen(url)
    soup = BeautifulStoneSoup(response.read())

    # then collect all the meta information
    tumblelog = soup.find("tumblelog")
    title = tumblelog["title"]
    description = tumblelog.string

    # use it to create a generic header for all posts
    header = "<html><head><title>" + title + "</title></head><body>"
    header += "<h1>" + title + "</h1><p>" + unescape(description) + "</p>"

    # then find the total number of posts
    posts_tag = soup.find("posts")
    total_posts = int(posts_tag["total"])

    # then get the XML files from the API, which we can only do with a max of 50 posts at once
    for i in range(0, total_posts, 50):
        # find the upper bound
        j = i + 49
        if j > total_posts:
            j = total_posts

        print "Getting posts " + str(i) + " to " + str(j) + "."

        url = "http://" + account + TUMBLR_URL + "?num=50&start=" + str(i)
        response = urllib2.urlopen(url)
        soup = BeautifulStoneSoup(response.read())

        posts = soup.findAll("post")
        for post in posts:
            savePost(post, header, save_folder)

    print "Backup Complete"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print "Invalid command line arguments. Please supply the name of your Tumblr account."
    else:
        account = sys.argv[1]
        backup(account)


