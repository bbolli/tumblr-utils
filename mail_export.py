#!/usr/bin/env python3

"""E-mails a user's recent Tumblr links to recipients.

I use this to automate the distribution of interesting links to
my geek-buddies mailing list.

- tag your links with a special tag
- run this script with your tumblr blog name, this tag and the recipient(s)
  as arguments every hour or so

The script needs write permissions in /var/local to save the ID of the
most recently mailed link. This ID is saved independently per user and tag.
"""

import base64
import os
import re
import smtplib
import textwrap
import urllib.parse
import urllib.request
from email.message import EmailMessage
import json


# configuration
SMTP_SERVER = 'localhost'
SENDER = base64.b64decode(b'bWVAZHJiZWF0Lmxp').decode()


class TumblrToMail:

    def __init__(self, user, tag, recipients):
        self.user = self.domain = user
        if '.' not in self.domain:
            self.domain += '.tumblr.com'
        self.tag = tag
        self.recipients = recipients
        self.db_file = os.path.expanduser('~/.config/tumblr_mail.latest')
        self.db_key = (user, tag)
        try:
            self.db = eval(open(self.db_file).read(), {}, {})
        except Exception:
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
        url = 'https://%s/api/read/json?type=link&filter=text' % self.domain
        posts = urllib.request.urlopen(url).read()
        posts = re.sub(rb'^.*?(\{.*\});*$', r'\1', posts)   # extract the JSON structure
        try:
            posts = json.loads(posts)
        except ValueError:
            print(posts)
            return []
        return [
            p for p in posts['posts']
            if int(p['id']) > self.latest and self.tag in p.get('tags', [])
        ]

    def make_mail(self, link):
        url = list(urllib.parse.urlsplit(link['link-url']))
        url[2] = urllib.parse.quote(url[2])
        mail = self.lw.fill('%s: %s' % (link['link-text'], urllib.parse.urlunsplit(url)))
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
            print(body)
            return

        msg = EmailMessage()
        msg.set_content(body, cte='quoted-printable')
        msg['Subject'] = "Interesting links" if len(links) > 1 else links[0]['link-text']
        msg['From'] = '%s (%s)' % (SENDER, self.user)
        if self.recipients:
            msg['To'] = ', '.join(self.recipients)

        if options.full:
            print(str(msg))
            return

        smtp = smtplib.SMTP(SMTP_SERVER)
        smtp.sendmail(SENDER, self.recipients, msg.as_string())
        smtp.quit()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Sends an email generated from tagged link posts.",
    )
    parser.add_argument('-d', '--dry-run', action='store_true',
        help="don't save which link was sent last"
    )
    parser.add_argument('-f', '--full', action='store_true',
        help="print the full mail with headers"
    )
    parser.add_argument('user', help="The Tumblr user or custom domain name")
    parser.add_argument('tag', help="The tag to filter for")
    parser.add_argument('recipients', nargs='*',
        help="The email recipients (if none, print the email body)"
    )

    options = parser.parse_args()
    TumblrToMail(options.user, options.tag, options.recipients).run(options)


if __name__ == '__main__':
    main()
