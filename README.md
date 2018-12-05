# tumblr-utils

This is a collection of utilities dealing with Tumblr blogs.

- `tumble.py` creates new posts from RSS or Atom feeds
- `tumblr_backup.py` makes a local backup of posts and images
- `mail_export.py` mails tagged links to a recipient list

These scripts are or have been useful to me over the years.

More documentation can be found in each script's docstring or in
[tumblr_backup.md](https://github.com/bbolli/tumblr-utils/blob/master/tumblr_backup.md).

The utilities run under Python 2.7.

### Notice

On 2015-06-04, I made the v2 API the default on the master branch. The former
master branch using the v1 API is still available on Github as `api-v1`, but
will no longer be updated. The one feature that's only available with the old
API is the option to backup password-protected blogs. There's no way to pass
a password in Tumblr's v2 API.

### License

[GPL3](http://www.gnu.org/licenses/gpl-3.0.txt).
