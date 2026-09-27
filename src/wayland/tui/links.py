"""Web link detection and conservative URL validation for chat clicks."""

import re
from urllib.parse import urlsplit


def web_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def link_ranges(text):
    result = []
    for match in re.finditer(r'https?://[^\s<>"`]+', text):
        url = match[0].rstrip(".,;:!?")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if web_url(url):
            result.append((match.start(), match.start() + len(url), url))
    return result


def slice_links(links, start, end):
    return [
        (max(left, start) - start, min(right, end) - start, url)
        for left, right, url in links
        if left < end and right > start
    ]
