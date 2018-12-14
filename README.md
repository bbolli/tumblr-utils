# tumblr-utils

This is a collection of utilities dealing with Tumblr blogs.

#### Before creating an issue, please read the documentation!
&nbsp;
#### There are 3 utilities:

- `tumblr_backup.py` makes a local backup of posts and images
    - Documentation is [here](tumblr_backup.md)
    - A step-by-step guide for beginners is [here](tumblr_backup_for_beginners.md)
- `tumble.py` creates new posts from RSS or Atom feeds
- `mail_export.py` mails tagged links to a recipient list

Documentation for `tumble.py` and `mail_export.py` can be found in each script's docstring.

Python 2.7 is required for these scripts.  Do not use Python 3!

&nbsp;
#### Features of tumblr_backup:
- Backs up images, both inline and photo posts
- Backs up videos (including YouTube and others with youtube-dl)
- Backs up audio (including SoundCloud)
- Backs up reblogs by default
- Supports downloading a blog's likes (experimental)
- Posts are backed up as minimally styled HTML5
- Indexing by month and by tag

&nbsp;
### Notice

On 2015-06-04, I made the v2 API the default on the master branch. The former
master branch using the v1 API is still available on Github as `api-v1`, but
will no longer be updated. The one feature that's only available with the old
API is the option to backup password-protected blogs. There's no way to pass
a password in Tumblr's v2 API.

### License

[GPL3](http://www.gnu.org/licenses/gpl-3.0.txt).
