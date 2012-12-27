#!/usr/bin/env python

"""E-mails a user's recent Tumblr links to recipients.

I use this to automate the distribution of interesting links to
my geek-buddies mailing list.

- tag your links with a special tag
- run this script with your tumblr blog name, this tag and the recipient(s)
  as arguments every hour or so

The script needs write permissions in /var/local to save the ID of the
most recently mailed link. This ID is saved independently per user and tag.
"""

# configuration
SMTP_SERVER = 'localhost'
SENDER = 'bbolli@ewanet.ch'


import urllib
import urlparse
import re
import smtplib
import textwrap
from email.mime.text import MIMEText
try:
    import json
except ImportError:
    # Python 2.5 and earlier need this package
    import simplejson as json


class TumblrToMail:

    def __init__(self, user, tag, recipients):
        self.user = self.domain = user
        if '.' not in self.domain:
            self.domain += '.tumblr.com'
        self.tag = tag
        self.recipients = recipients
        self.db_file = '/var/local/tumblr_mail.latest'
        self.db_key = (user, tag)
        try:
            self.db = eval(open(self.db_file).read(), {}, {})
        except (IOError, OSError):
            self.db = {}
        self.latest = self.db.get(self.db_key, 0)
        self.lw = textwrap.TextWrapper(initial_indent='* ', subsequent_indent='  ',
            break_long_words=False, break_on_hyphens=False
        )
        self.tw = textwrap.TextWrapper(initial_indent='  ', subsequent_indent='  ')

    def __del__(self):
        if self.latest:
            self.db[self.db_key] = self.latest
            open(self.db_file, 'w').write(repr(self.db))

    def get_links(self):
        url = 'http://%s/api/read/json?type=link&filter=text' % self.domain
        posts = urllib.urlopen(url).read()
        posts = re.sub(r'^.*?(\{.*\});*$', r'\1', posts)   # extract the JSON structure
        try:
            posts = json.loads(posts)
        except ValueError:
            print posts
            return []
        return [
            p for p in posts['posts']
            if int(p['id']) > self.latest and self.tag in p.get('tags', [])
        ]

    def make_mail(self, link):
        url = list(urlparse.urlsplit(link['link-url']))
        url[2] = urllib.quote(url[2])
        mail = self.lw.fill(u'%s: %s' % (link['link-text'], urlparse.urlunsplit(url)))
        desc = link['link-description']
        if desc:
            mail += '\n\n' + self.tw.fill(desc)
        return mail

    def run(self, options):
        links = self.get_links()
        if not links:
            return

        body = ('\n\n'.join(self.make_mail(l) for l in links)).strip() + """

-- 
http://%s
""" % self.domain

        self.latest = max(int(l['id']) for l in links) if not options.dry_run else None

        if not self.recipients and not options.full:
            print body
            return

        msg = MIMEText(body.encode('utf-8'))
        msg.set_charset('utf-8')
        msg['Subject'] = "Interesting links" if len(links) > 1 else links[0]['link-text']
        msg['From'] = '%s (%s)' % (SENDER, self.user)
        if self.recipients:
            msg['To'] = ', '.join(self.recipients)

        if options.full:
            print msg.as_string()
            return

        smtp = smtplib.SMTP(SMTP_SERVER)
        smtp.sendmail(SENDER, self.recipients, msg.as_string())
        smtp.quit()


if __name__ == '__main__':
    import optparse
    parser = optparse.OptionParser("Usage: %prog [options] blog-name tag [recipient ...]",
        description="Sends an email generated from tagged link posts.",
        epilog="Without recipients, prints the mail body to stdout."
    )
    parser.add_option('-d', '--dry-run', action='store_true',
        help="don't save which link was sent last"
    )
    parser.add_option('-f', '--full', action='store_true',
        help="print the full mail with headers to stdout"
    )
    options, args = parser.parse_args()
    try:
        user = args[0]
        tag = args[1]
        recipients = args[2:]
    except IndexError:
        parser.error("blog-name and tag are required arguments.")

    TumblrToMail(user, tag, recipients).run(options)
