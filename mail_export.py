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
        self.user = user
        self.tag = tag
        self.recipients = recipients
        self.db_file = '/var/local/tumblr_mail.latest'
        self.db_key = (user, tag)
        try:
            self.db = eval(open(self.db_file).read())
        except (IOError, OSError):
            self.db = {}
        self.latest = self.db.get(self.db_key, 0)
        self.lw = textwrap.TextWrapper(initial_indent='* ', subsequent_indent='  ',
            break_long_words=False, break_on_hyphens=False
        )
        self.tw = textwrap.TextWrapper(initial_indent='  ', subsequent_indent='  ')

    def __del__(self):
        self.db[self.db_key] = self.latest
        open(self.db_file, 'w').write(repr(self.db))

    def get_links(self):
        url = 'http://%s.tumblr.com/api/read/json' % self.user
        posts = urllib.urlopen(url).read()
        posts = re.sub(r'^.*?(\{.*\});*$', r'\1', posts)   # extract the JSON structure
        posts = json.loads(posts)
        return [
            p for p in posts['posts']
            if int(p['id']) > self.latest and p['type'] == 'link'
            and self.tag in p['tags']
        ]

    def make_mail(self, link):
        url = list(urlparse.urlsplit(link['link-url']))
        url[2] = urllib.quote(url[2])
        mail = self.lw.fill(u'%s: %s' % (link['link-text'], urlparse.urlunsplit(url)))
        desc = link['link-description']
        if desc:
            if link['format'] == 'html':
                # FIXME: poor man's HTML to text conversion
                desc = re.sub(r'<.*?>', ' ', desc)
                desc = re.sub(r'\s+', ' ', desc).strip()
                desc = re.sub(r'&#(\d+);', lambda m: unichr(int(m.group(1))), desc)
                desc = re.sub(r'(?i)&#x([0-9a-f]+);', lambda m: unichr(int(m.group(1), 16)), desc)
            mail += '\n\n' + self.tw.fill(desc)
        return mail

    def run(self):
        links = self.get_links()
        if not links:
            return

        body = ('\n\n'.join(self.make_mail(l) for l in links)).strip() + """

-- 
http://%s.tumblr.com
""" % self.user

        msg = MIMEText(body.encode('utf-8'))
        msg.set_charset('utf-8')
        msg['Subject'] = "Interesting links" if len(links) > 1 else links[0]['link-text']
        msg['From'] = '%s (%s)' % (SENDER, self.user)
        msg['To'] = ', '.join(self.recipients)

        smtp = smtplib.SMTP(SMTP_SERVER)
        smtp.sendmail(SENDER, self.recipients, msg.as_string())
        smtp.quit()

        self.latest = max(int(l['id']) for l in links)


if __name__ == '__main__':
    import sys
    try:
        user = sys.argv[1]
        tag = sys.argv[2]
        recipients = sys.argv[3:]
        if not recipients:
            raise IndexError('Missing recipient')
    except IndexError:
        sys.stderr.write('Usage: %s user tag recipient...\n' % sys.argv[0])
        sys.exit(1)

    TumblrToMail(user, tag, recipients).run()
