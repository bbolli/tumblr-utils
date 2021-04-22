# tumblr-utils

### About this fork

This fork is focused on tumblr_backup.py. It adds Python 3 compatibility,
various bug fixes, a few enhancements to normal operation, support for
dashboard-only blogs, and several other features - see the output of
`tumblr_backup.py --help` for the full list of options. Check out the
"experimental" branch if you want to try out some less stable extra
functionality.

---

This is a collection of utilities dealing with Tumblr blogs.

- `tumble.py` creates new posts from RSS or Atom feeds
- `tumblr_backup.py` makes a local backup of posts and images
- `mail_export.py` mails tagged links to a recipient list

These scripts are or have been useful to me over the years.

More documentation can be found in each script's docstring or in
[tumblr_backup.md](https://github.com/bbolli/tumblr-utils/blob/master/tumblr_backup.md).

The utilities run under Python 2.7, though Python 3 is supported and preferred
for tumblr_backup.py, which has been tested on Python 3.8 but should also run
fine on Python 3.9.3 and later.

### Notice

On 2015-06-04, I made the v2 API the default on the master branch. The former
master branch using the v1 API is still available on Github as `api-v1`, but
will no longer be updated. The one feature that's only available with the old
API is the option to backup password-protected blogs. There's no way to pass
a password in Tumblr's v2 API.

### License

[GPL3](http://www.gnu.org/licenses/gpl-3.0.txt).
