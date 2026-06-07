"""
text_parser.py — URL and @mention extraction from post text.

Faithful port of the C++ regex patterns used in TextHandler.h:

  URL pattern   : matches http:// and https:// URLs
                  C++ uses std::regex with pattern
                  "(http://|https://)[a-zA-Z0-9\\-\\.]+\\.[a-zA-Z]{2,3}(/\\S*)?"

  Mention pattern: matches @username tokens
                  C++ uses std::regex with pattern
                  "@[a-zA-Z0-9-_]+"

Both are extracted from the raw post text before the text is modified.
The URL extraction result is used to call UrlShortenService; the mention
result is used to call UserMentionService.  The modified text has each
original URL replaced with its shortened form.
"""

import re
from dataclasses import dataclass

# Mirrors the C++ std::regex pattern for URLs
_URL_PATTERN = re.compile(
    r'https?://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,3}(?:/\S*)?'
)

# Mirrors the C++ std::regex pattern for @mentions
_MENTION_PATTERN = re.compile(
    r'@[a-zA-Z0-9_\-]+'
)


@dataclass
class ParsedText:
    """Result of parsing raw post text."""
    urls:      list[str]   # raw expanded URLs found in text
    usernames: list[str]   # mention usernames (without the '@')


def extract_urls(text: str) -> list[str]:
    """Return all URL strings found in text (preserving order, with duplicates)."""
    return _URL_PATTERN.findall(text)


def extract_usernames(text: str) -> list[str]:
    """
    Return all @mention username strings found in text (without '@').
    Preserves order; duplicates kept (de-dup handled upstream if needed).
    """
    return [m[1:] for m in _MENTION_PATTERN.findall(text)]


def parse(text: str) -> ParsedText:
    """Extract both URLs and @mention usernames from text."""
    return ParsedText(
        urls=extract_urls(text),
        usernames=extract_usernames(text),
    )


def replace_urls(text: str, url_map: dict[str, str]) -> str:
    """
    Replace each expanded URL in text with its shortened form.

    url_map: { expanded_url -> shortened_url }

    Uses a single regex pass to replace all occurrences, matching the C++
    std::regex_replace behaviour.
    """
    if not url_map:
        return text

    def _replacer(match: re.Match) -> str:
        original = match.group(0)
        return url_map.get(original, original)

    return _URL_PATTERN.sub(_replacer, text)
