tumblr_backup
=============

0. What is this?
----------------

`tumblr_backup` is a Python script that backs up your
[Tumblr](http://www.tumblr.com) blog locally. The backup includes all
images from image posts. An index links to monthly pages, which contain
all the posts from the respective month, plus links to single post pages.
There are many options to select the posts to backup.

You can see en example of `tumblr_backup`’s output
[on my home page](http://drbeat.li/tumblr).


1. Installation
---------------

1. Download and unzip
   [xmltramp.zip](https://github.com/bbolli/xmltramp/zipball/master).
2. Install `xmltramp.py` somewhere on your Python path like
   `/usr/local/lib/python2.6/dist-packages`.
3. Download and unzip
   [tumblr_backup.zip](https://github.com/bbolli/tumblr_backup/zipball/bb).
4. Copy `tumblr_backup.py` to a directory on your `$PATH` like `~/bin` or
   `/usr/local/bin`.
5. Run `tumblr_backup.py` _blog-name_ as often as you like manually
   or from a cron job.


2. Usage
--------

### 2.1. Synopsis

    tumblr_backup.py [-q] [-n post-count] [-s start-post] [-p y|m|d|YYYY[MM[DD]]] [-t] [blog-name] ...

### 2.2. Options

* `-q`: Suppress the progress display.
* `-n` _post-count_: Stop backing up after _post-count_ posts.
* `-s` _start-post_: Start backing up at the _start-post_’th post.
* `-p` _period_: Limit the backup to the given period.
  These are ways to define the period:
  * `y`: the current year
  * `m`: the current month
  * `d`: the current day (i.e. today ;-)
  * _yyyy_: the given year
  * _yyyy-mm_: the given month
  * _yyyy-mm-dd_: the given day
* `-t`: Include the theme in the backup

### 2.3. Arguments

* _blog-name_: The name of your blog.

If your blog is under `.tumblr.com`, you can give just the first domain name
part; if your blog is under your own domain, give the whole domain name.
You can give more than one _blog-name_ to backup multiple blogs in one go.

The default blog name can be changed in the script.


3. Operation
------------

By default, `tumblr_backup` backs up all posts.

The generated directory structure looks like this:

    ./ - the current directory
        <blog-name>/ - your blog backup
            index.html - table of contents with links to the monthly pages
            archive/
                <yyyy-mm>.html - the monthly pages
                …
                period-<yyyy>.html - the index of a yearly period
                period-<yyyy-mm>.html - the index of a monthly period
                period-<yyyy-mm-dd>.html - the index of a single day
                …
            posts/
                <id>.html - the single post pages
                …
            images/
                <image.ext> - the image files
                …
            theme/
                _local.css - the local style sheet
                theme.html - the saved HTML template
                custom.css - the CSS customizations
                avatar.<ext> - your avatar image

The name of the single post pages is the numeric post id.  The modification
time of the single post pages is set to the post’s timestamp. `tumblr_backup`
applies a simple style to the saved pages. All generated pages are
[HTML5](http://www.whatwg.org/specs/web-apps/current-work/multipage/).

Tumblr saves most image files without extension. This probably saves a few
million bytes in their database. `tumblr_backup` restores the image extensions.
If an image is already backed up, it is not downloaded again. The image
extension determination only downloads the first 32 bytes of the image.

In order to limit the set of backed up posts, use the `-n` and `-s` options.
The most recent post is always number 0, so the option `-n 200` would select
the 200 most recent posts. Calling `tumblr_backup -n 100 -s 200` would skip
the 200 most recent posts and backup the next 100. The generated index will
just contain links to the posts selected.

With option `-p`, a separate archive page is generated for the
selected period. This page has the name `period-`_period_`.html`.
Any other index or archive pages are left alone.

If you combine `-n` and/or `-s` with `-p`, only posts matching both criteria
will be backed up.

In order to successfully backup your blog theme with `-t`, you need to define
your Tumblr login information in the file `~/.netrc` with an entry like this:

`machine www.tumblr.com login` _login-email_ `password` _tumblr-password_

This file should have mode 0600 (read/write by owner only).

All operations except `-t` use only public Tumblr APIs, so you can use the
program to backup blogs that you don’t own.


4. Changelog
------------

See [here](https://github.com/bbolli/tumblr_backup/commits/bb). There are no
formal releases; so check back often!


5. Author
---------

Beat Bolli `<me+tumblr_backup@drbeat.li>`, [http://drbeat.li](http://drbeat.li)
