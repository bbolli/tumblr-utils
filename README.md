# tumblr-backup

### About this fork

This is a fork of bbolli's
[tumblr-utils](https://github.com/bbolli/tumblr-utils), with a focus on
tumblr\_backup.py. It adds Python 3 compatibility, various bug fixes, a few
enhancements to normal operation, support for dashboard-only blogs, and several
other features - see the output of `tumblr-backup --help` for the full list of
options.

---

## 0. Description

tumblr-backup is a script that backs up your [Tumblr](http://tumblr.com) blog
locally.

The backup includes all images both from inline text as well as photo posts. An
index links to monthly pages, which contain all the posts from the respective
month with links to single post pages. Command line options select which posts
to backup and set the output format. The audio and video files can also be
saved.

By default, all posts of a blog are backed up in minimally styled HTML5.

You can see an example of its output [on my home page](http://drbeat.li/tumblr).


## 1. Installation

1. `pip install tumblr-backup`
2. Create an "app" at https://www.tumblr.com/oauth/apps. Follow the instructions
   there; most values entered don't matter.
3. `tumblr-backup --set-api-key API_KEY`, where API\_KEY is the OAuth Consumer
   Token from the app created in the previous step.
4. Run `tumblr-backup blog-name` as often as you like manually or from a cron
   job.

There are several optional dependencies that enable additional features:

1. To backup audio and video, install `tumblr-backup[video]`, or you can
   manually install either yt-dlp or youtube\_dl. If you need HTTP cookies to
   download, use an appropriate browser plugin to extract the cookie(s) into a
   file and use option `--cookiefile=file`. See
   [issue 132](https://github.com/bbolli/tumblr-utils/issues/132).
2. To enable EXIF tagging, install `tumblr-backup[exif]`, or you can manually
   install py3exiv2.
3. To back up notes with the --save-notes option, install
   `tumblr-backup[notes]`, or you can manually install beautifulsoup4 and lxml.
4. To use the -F/--filter option to filter the downloaded posts with arbitrary
   rules based on their metadata, install `tumblr-backup[jq]`. Alternatively,
   you can manually install the [jq](https://github.com/mwilliamson/jq.py)
   module.
5. To install tumblr-backup with all optional features available, use
   `pip install tumblr-backup[all]`.


## 2. Usage

### Synopsis

    tumblr-backup [options] blog-name ...

### Options

```
positional arguments:
  blogs

options:
  -h, --help            show this help message and exit
  -O OUTDIR, --outdir OUTDIR
                        set the output directory (default: blog-name)
  -D, --dirs            save each post in its own folder
  -q, --quiet           suppress progress messages
  -i, --incremental     incremental backup mode
  -l, --likes           save a blog's likes, not its posts
  -k, --skip-images     do not save images; link to Tumblr instead
  --save-video          save all video files
  --save-video-tumblr   save only Tumblr video files
  --save-audio          save audio files
  --save-notes          save a list of notes for each post
  --copy-notes          copy the notes list from a previous archive (inverse:
                        --no-copy-notes)
  --notes-limit COUNT   limit requested notes to COUNT, per-post
  --cookiefile COOKIEFILE
                        cookie file for youtube-dl, --save-notes, and svc API
  -j, --json            save the original JSON source
  -b, --blosxom         save the posts in blosxom format
  -r, --reverse-month   reverse the post order in the monthly archives
  -R, --reverse-index   reverse the index file order
  --tag-index           also create an archive per tag
  -a HOUR, --auto HOUR  do a full backup at HOUR hours, otherwise do an
                        incremental backup (useful for cron jobs)
  -n COUNT, --count COUNT
                        save only COUNT posts
  -s SKIP, --skip SKIP  skip the first SKIP posts
  -p PERIOD, --period PERIOD
                        limit the backup to PERIOD ('y', 'm', 'd',
                        YYYY[MM[DD]][Z], or START,END)
  -N COUNT, --posts-per-page COUNT
                        set the number of posts per monthly page, 0 for
                        unlimited
  -Q REQUEST, --request REQUEST
                        save posts matching the request
                        TYPE:TAG:TAG:…,TYPE:TAG:…,…. TYPE can be text, quote,
                        link, answer, video, audio, photo, chat or any; TAGs
                        can be omitted or a colon-separated list. Example: -Q
                        any:personal,quote,photo:me:self
  -t REQUEST, --tags REQUEST
                        save only posts tagged TAGS (comma-separated values;
                        case-insensitive)
  -T REQUEST, --type REQUEST
                        save only posts of type TYPE (comma-separated values
                        from text, quote, link, answer, video, audio, photo,
                        chat)
  -F FILTER, --filter FILTER
                        save posts matching a jq filter (needs jq module)
  --no-reblog           don't save reblogged posts
  --only-reblog         save only reblogged posts
  -I FMT, --image-names FMT
                        image filename format ('o'=original, 'i'=<post-id>,
                        'bi'=<blog-name>_<post-id>)
  -e KW, --exif KW      add EXIF keyword tags to each picture (comma-separated
                        values; '-' to remove all tags, '' to add no extra
                        tags)
  -S, --no-ssl-verify   ignore SSL verification errors
  --prev-archives DIRS  comma-separated list of directories (one per blog)
                        containing previous blog archives
  --no-post-clobber     Do not re-download existing posts
  --no-server-timestamps
                        don't set local timestamps from HTTP headers
  --hostdirs            Generate host-prefixed directories for media
  --user-agent USER_AGENT
                        User agent string to use with HTTP requests
  --skip-dns-check      Skip DNS checks for internet access
  --threads THREADS     number of threads to use for post retrieval
  --continue            Continue an incomplete first backup
  --ignore-diffopt      Force backup over an incomplete archive with different
                        options
  --no-get              Don't retrieve files not found in --prev-archives
  --reuse-json          Reuse the API responses saved with --json (implies
                        --copy-notes)
  --internet-archive    Fall back to the Internet Archive for Tumblr media 403
                        and 404 responses
  --media-list          Save post media URLs to media.json
  --id-file FILE        file containing a list of post IDs to save, one per
                        line
  --json-info           Just print some info for each blog, don't make a
                        backup
```

### Arguments

_blog-name_: The name of the blog to backup.

If your blog is under `.tumblr.com`, you can give just the first domain name
part; if your blog is under your own domain, give the whole domain name. You
can give more than one _blog-name_ to backup multiple blogs in one go.

The default blog name(s) can be changed by copying `settings.py.example` to
`settings.py` and adding the name(s) to the `DEFAULT_BLOGS` list.

### Environment variables

`LC_ALL`, `LC_TIME`, `LANG`: These variables, in decreasing importance,
determine the locale for month names and the date/time format.

### Exit code

The exit code is 0 if at least one post has been backed up, 1 if no post has
been backed up, 2 on invocation errors, 3 if the backup was interrupted, or 4
on HTTP errors.


## 3. Operation

By default, tumblr-backup backs up all posts in HTML format.

The generated directory structure looks like this:

    ./ - the current directory
        <outdir>/ - your blog backup
            index.html - table of contents with links to the monthly pages
            backup.css - the default backup style sheet
            custom.css - the user's style sheet (optional)
            override.css - the user's style sheet override (optional)
            archive/
                <yyyy-mm-pnn>.html - the monthly pages
                …
            posts/
                <id>.html - the single post pages
                …
            media/
                <image.ext> - image files
                <audio>.mp3 - audio files
                <video>.mp4 - video files
                …
            json/
                <id>.json - the original JSON posts
                …
            tags/
                index.html - the index of all tag indices
                <tag>/index.html - the index for <tag>
                    archive/
                        <yyyy-mm-pnn>.html - the monthly pages for <tag>
            theme/
                avatar.<ext> - the blog’s avatar
                style.css - the blog’s style sheet

The default `outdir` is the `blog-name`.

If option `-D` is used, one folder per post is generated, and the post's
images are saved in the same folder. The monthly archive is also stored in a
folder per month. This results in the same URL structure as on the Tumblr page.

The directories look like this:

    ./ - the current directory
        <outdir>/ - your blog backup
            index.html - table of contents with links to the monthly pages
            backup.css - the default backup style sheet
            custom.css - the user's style sheet (optional)
            override.css - the user's style sheet override (optional)
            archive/
                <yyyy-mm-pnn>/
                    index.html - the monthly page
                …
            posts/
                <id>/
                    index.html - the single post page
                    <image.ext> - the image file(s) for this post
                    <audio>.mp3 - audio files
                    <video>.mp4 - video files
                    …
                …
            json/
                <id>.json - the original JSON posts
                …
            theme/
                avatar.<ext> - the blog’s avatar
                style.css - the blog’s style sheet

The modification time of the single post pages is set to the post’s timestamp.
tumblr-backup applies a simple style to the saved pages. All generated pages are
[HTML5](http://html5.org).

The index pages are recreated from scratch after every backup, based on the
existing single post pages. Normally, the index and monthly pages are in
reverse chronological order, i.e. more recent entries on top. The options `-R`
and `-r` can be used to reverse the order.

Option `--tag-index` creates a tag index for each tag used in the posts.
It can be reached through the "Tag index" link in the main index.

If you want to use a custom CSS file, call it `custom.css`, put it in the backup
folder and do a complete backup. Without a custom CSS file, tumblr-backup saves
a default style sheet in `backup.css`. The blog's style sheet itself is always
saved in `theme/style.css`.

It you want to override just a few default styles, create the file
`override.css` in the backup folder. This file is included automatically by the
default style sheet. You may have to mark your overriding styles with
`!important` to make them stick because `override.css` is imported first in the
style sheet.

Tumblr saves some image files without extension. This probably saves a few
billion bytes in their database. tumblr-backup restores the image extensions. If
an image is already backed up, it is not downloaded again. If an image is
re-uploaded/edited, the old image is kept in the backup, but no post links to
it. The format of the image file names can be selected with the `-I` option.

It must be noted that saved inline images (from non-photo posts) keep their
name. This means that only the first image with any given name will be saved;
the others with the same name will point to the first one.

The download of images can be disabled with option `-k`. In this case, the
image URLs will point to the original location.

With option `-e`, IPTC keyword tags can be added to image files. There are
three possibilities:

1. `-e kw1,kw2` adds the post's tags plus `kw1` and `kw2` as keywords
2. `-e ''` adds just the post's tags
3. `-e -` removes all keywords from the image

In incremental backup mode, tumblr-backup saves only posts that have higher ids
than the highest id saved locally. Note that posts that are edited after being
backed up are not backed up again with this option.

In JSON backup mode, the original JSON source returned by the Tumblr API is
saved under the `json/` folder in addition to the HTML format.

Automatic archive mode `-a` is designed to be used from an hourly cron script.
It normally makes an incremental backup except if the current hour is the one
given as argument. In this case, tumblr-backup will make a full backup. An
example invocation is `tumblr-backup -qa4` to do a full backup at 4 in the
morning. This option obviates the need for shell script logic to determine what
options to pass. If you don't want cron to send a mail if no new posts have been
backed up, use this crontab entry:

    0 * * * * tumblr-backup -qa4 <blog-name> || test $? -eq 1

This changes the exit code 1 to 0.

In Blosxom format mode, the posts generated are saved in a format suitable for
re-publishing in [Blosxom](http://www.blosxom.com) with the [Meta
plugin](http://www.blosxom.com/plugins/meta/meta.htm). Images are not
downloaded; instead, the image links point back to the original image on
Tumblr. The posts are saved in the current folder with a `.txt` extension. The
index is not updated.

In order to limit the set of backed up posts, use the `-n` and `-s` options. The
most recent post is always number 0, so the option `-n 200` would select the 200
most recent posts. Calling `tumblr-backup -n 100 -s 200` would skip the 200 most
recent posts and backup the next 100. `-n 1` is the fastest way to rebuild the
index pages.

The option `-T` limits the backup to posts of the given type. `-t` saves only
posts with the given tags. `-Q` combines both: it accepts comma-separated
requests of the form `TYPE:TAG1:TAG2:…`, where the tags for each post type can
be different. Omitting the TAGs is allowed; this saves posts of this type with
any or no tags. Example: `-Q any:personal,quote,photo:me:self` saves all posts
tagged 'personal', all quotes, and photos tagged 'me' or 'self' or 'personal'
(because of the `any` request).

The option `--no-reblog` suppresses the backup of reposts of other blogs'
posts.

If you combine `-n`, `-s`, `-i`, `-p`, `-t`, `-T`, `-Q` and `--no-reblog`, only
posts matching all criteria will be backed up.

All options use only public Tumblr APIs, so you can use the program to backup
blogs that you don’t own.

tumblr-backup is developed and tested on Linux and OS X. If you want to run it
under Windows, I suggest to try the excellent [Cygwin](http://cygwin.com)
environment.


## 4. Changelog

See [here](https://github.com/cebtenzzre/tumblr-utils/commits).


## 5. Acknowledgments

- [bdoms](https://github.com/bdoms/tumblr_backup) for the initial implementation
- [WyohKnott](https://github.com/WyohKnott) for numerous bug reports and patches
- [Tumblr](https://www.tumblr.com) for their discontinued backup tool whose
  output was the inspiration for the styling applied in `tumblr_backup`.
- [Beat Bolli](https://github.com/bbolli/tumblr-utils)
