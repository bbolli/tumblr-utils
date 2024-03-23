from __future__ import annotations

import re
from typing import Any, Callable


def _check_posted_note(doc: dict[str, Any]) -> bool:
    notes = doc.get('notes')
    if not (notes and isinstance(notes, list)):
        return False  # no notes available

    n = notes[-1]
    return bool(
        n['type'] == 'posted'
        and n['timestamp'] < doc['timestamp']  # sometimes a later reblog is credited
        and n['blog_uuid'] != doc['blog']['uuid'],
    )


def _check_content(doc: dict[str, Any], pred: Callable[[str], bool], name: str) -> bool:
    reblog_info = doc.get('reblog', {})

    if doc.get('is_submission') and not reblog_info.get('tree_html'):
        return False  # prone to false-positives
    if 'post_html' in doc:
        return False  # post_html is messy and we have root_id anyway

    # reason: quote source content
    if 'source' in doc:
        return name == 'via' and pred(doc['source'])  # this key is more specific

    # reason: comment content
    return bool(
        reblog_info
        and (name == 'via' or not reblog_info['tree_html'])
        and pred(reblog_info['comment']),
    )


BQ_RE = re.compile(
    r'('
      r'<(?!a[ >])[^<>]+>'
      r'|'
      r'(?![^>\n\s][^\S\n]*<a[ >])[^<>]'
    r')*'
    r'<a('
      r' class="(?P<classes>[^"]*)"'
      r'|'
      r' href="https?://('
        r'(?P<blogco>tmblr\.co/[a-zA-Z0-9_]+/?)'
        r'|'
        r'www\.tumblr\.com/dashboard/blog/(?P<bname0>[a-zA-Z0-9-]+)/[0-9]+/?'
        r'|'
        r'(?P<priv>www\.tumblr\.com/blog/private_[0-9]+\?[0-9]+)'
        r'|'
          r'('
            r'(www|(?P<bname1>[a-zA-Z0-9-]+))\.tumblr.com'
            r'|'
            r'[^/"]+'
          r')'
          r'('
            r'(?P<blogpost>/post/[0-9]+(/[^/"]*)?)'
            r'|'
            r'/[^"]*'  # poster-editable
          r')?'
      r')"'
      r'|'
      r' [^\s</>"' "'" r'=]+(="[^"]*"|\b)'
    r')*'
    r'>'
      r'[^<>]*'  # poster-editable
    r'</a>:'
    r'(?![^\S\n]*[^<\s])',
)
BQ_RE2 = re.compile(r'(<p>)+[a-z0-9-]+:</p>\n*<blockquote>')


def bqpred(c: str) -> bool:
    if 'replied to your' in c:
        return False
    if BQ_RE2.match(c):
        return True
    m = BQ_RE.match(c)
    if not m:
        return False
    return bool(
        'tumblr_blog' in (m.group('classes') or '').split(' ')
        or m.group('blogpost') or m.group('priv') or m.group('bname0')
        or ((m.group('blogco') or m.group('bname1')) and re.search(r'<blockquote[ >]', c)),
    )


def post_is_reblog(doc: dict[str, Any]) -> bool:
    # reason: reblogged_from_id
    # true for 84.9% of posts, 99.7% of reblogs
    if 'reblogged_from_id' in doc:
        return True

    # reason: root_id
    # false for all svc reblogs (let's say 14.3% of posts)
    # true for 0.3% of remaining reblogs
    root = doc.get('root_id')
    if root:
        return int(root) != int(doc['id'])

    trail = doc.get('trail')
    if trail:
        # reason: trail first post ID
        # true for 95.6% of remaining reblogs
        if int(trail[0]['post']['id']) != int(doc['id']):
            return True

        # reason: missing trail root
        # true for 7.9% of remaining reblogs (and cheap)
        if not any(p.get('is_root_item') for p in trail):
            return True

    # true for 96.9% of remaining reblogs
    def viapred(c: str) -> bool:
        return bool(re.search(r'\(via <a (class="tumblr_blog" |href="https?://[^/]+/?"[ >])', c))
    if _check_content(doc, viapred, 'via'):
        return True

    # reason: posted note
    # true for 36.4% of remaining reblogs (and cheap)
    if _check_posted_note(doc):
        return True

    # reason: non-empty tree_html
    # true for 14.3% of remaining reblogs (and cheap)
    reblog_info = doc.get('reblog', {})
    if reblog_info.get('tree_html') and ' replied to your ' not in reblog_info['tree_html']:
        return True

    # true for all (known) remaining reblogs
    if _check_content(doc, bqpred, 'blockquote'):
        return True

    return False  # probably not a reblog
