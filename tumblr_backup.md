## 0. Description

`tumblr_backup.py` is a script that backs up your [Tumblr](http://tumblr.com)
blog locally.

The backup includes all images from photo and photoset posts. An index links to
monthly pages, which contain all the posts from the respective month with links
to single post pages. Command line options select which posts to backup and set
the output format.

By default, all posts of a blog are backed up in minimally styled HTML.

You can see an example of its output [on my home page](http://drbeat.li/tumblr).


## 1. Installation

1. Download and unzip
   [xmltramp.zip](https://github.com/bbolli/xmltramp/zipball/master).
2. Install `xmltramp.py` somewhere on your Python path like
   `/usr/local/lib/python2.6/dist-packages`.
3. Download and unzip
   [tumblr-utils.zip](https://github.com/bbolli/tumblr-utils/zipball/master)
   or clone the Github repo from `git://github.com/bbolli/tumblr-utils.git`.
4. Copy or symlink `tumblr_backup.py` to a directory on your `$PATH` like
   `~/bin` or `/usr/local/bin`.
5. Run `tumblr_backup.py` _blog-name_ as often as you like manually or from a
   cron job. The recommendation is to do a hourly incremental backup and a
   daily complete one.


## 2. Usage

### Synopsis

    tumblr_backup.py [options] blog-name ...

### Options

    -h, --help            show this help message and exit
    -q, --quiet           suppress progress messages
    -i, --incremental     incremental backup mode
    -x, --xml             save the original XML source
    -b, --blosxom         save the posts in blosxom format
    -r, --reverse-month   reverse the post order in the monthly archives
    -R, --reverse-index   reverse the index file order
    -a HOUR, --auto=HOUR  do a full backup at HOUR hours, otherwise do an
                          incremental backup (useful for cron jobs)
    -n COUNT, --count=COUNT
                          save only COUNT posts
    -s SKIP, --skip=SKIP  skip the first SKIP posts
    -p PERIOD, --period=PERIOD
                          limit the backup to PERIOD:
                            'y': the current year
                            'm': the current month
                            'd': the current day (i.e. today ;-)
                            YYYY: the given year
                            YYYY-MM: the given month
                            YYYY-MM-DD: the given day
    -P PASSWORD, --private=PASSWORD
                          password to a private tumblr

### Arguments

_blog-name_: The name of the blog to backup.

If your blog is under `.tumblr.com`, you can give just the first domain name
part; if your blog is under your own domain, give the whole domain name. You
can give more than one _blog-name_ to backup multiple blogs in one go.

The default blog name can be changed in the script.

### Environment variables

`LC_ALL`, `LC_TIME`, `LANG`: These variables, in decreasing importance,
determine the locale for month names and the date/time format.

### Exit code

The exit code is 0 if at least one post has been backed up, 1 if no post has
been backed up, and 2 on invocation errors.


## 3. Operation

By default, `tumblr_backup` backs up all posts in HTML format.

The generated directory structure looks like this:

    ./ - the current directory
        <blog-name>/ - your blog backup
            index.html - table of contents with links to the monthly pages
            backup.css - the default backup style sheet
            custom.css - the user's style sheet (optional)
            archive/
                <yyyy-mm>.html - the monthly pages
                …
            posts/
                <id>.html - the single post pages
                …
            images/
                <image.ext> - the image files
                …
            xml/
                <id>.xml - the original XML posts
                …
            theme/
                avatar.<ext> - the blog’s avatar
                style.css - the blog’s style sheet

The name of the single post pages is their numeric post id. The modification
time of the single post pages is set to the post’s timestamp. `tumblr_backup`
applies a simple style to the saved pages. All generated pages are
[HTML5](http://html5.org).

The index pages are recreated from scratch after every backup, based on the
existing single post pages. Normally, the index and monthly pages are in
reverse chronological order, i.e. more recent entries on top. The options `-R`
and `-r` can be used to reverse the order.

If you want to use a custom CSS file, call it `custom.css`, put it in the
backup folder and do a complete backup. Without a custom CSS file,
`tumblr_backup` saves a default style sheet in `backup.css`. The blog's style
sheet itself is always saved in `theme/style.css`.

Tumblr saves most image files without extension. This probably saves a few
million bytes in their database. `tumblr_backup` restores the image extensions.
If an image is already backed up, it is not downloaded again. If an image is
re-uploaded/edited, the old image is kept in the backup, but no post links to
it.

In incremental backup mode, `tumblr_backup` saves only posts that have higher
ids than the highest id saved locally. Note that posts that are edited after
being backed up are not backed up again with this option.

In XML backup mode, the original XML source returned by the Tumblr API is saved
under the `xml/` folder in addition to the HTML format.

Automatic archive mode `-a` is designed to be used from an hourly cron script.
It normally makes an incremental backup except if the current hour is the one
given as argument. In this case, `tumblr_backup` will make a full backup. An
example invocation is `tumblr_backup.py -qa4` to do a full backup at 4 in the
morning. This option obviates the need for shell script logic to determine what
options to pass.

In Blosxom format mode, the posts generated are saved in a format suitable for
re-publishing in [Blosxom](http://www.blosxom.com) with the [Meta
plugin](http://www.blosxom.com/plugins/meta/meta.htm). Images are not
downloaded; instead, the image links point back to the original image on
Tumblr. The posts are saved in the current folder with a `.txt` extension. The
index is not updated.

In order to limit the set of backed up posts, use the `-n` and `-s` options.
The most recent post is always number 0, so the option `-n 200` would select
the 200 most recent posts. Calling `tumblr_backup -n 100 -s 200` would skip the
200 most recent posts and backup the next 100. `-n 1` is the fastest way to
rebuild the index pages.

If you combine `-n`, `-s`, `-i` and `-p`, only posts matching all criteria will
be backed up.

All options use only public Tumblr APIs, so you can use the program to backup
blogs that you don’t own.

`tumblr_backup` is developed and tested on Linux and OS X. If you want to run
it under Windows, I suggest to try the excellent [Cygwin](http://cygwin.com)
environment.


## 4. Changelog

See [here](https://github.com/bbolli/tumblr-utils/commits/master/tumblr_backup.py).
There are no formal releases so check back often!


## 5. Author

Beat Bolli `<me+tumblr-utils@drbeat.li>`,
[http://drbeat.li/py/](http://drbeat.li/py/)
