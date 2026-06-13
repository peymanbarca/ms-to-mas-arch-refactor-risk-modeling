"""
tools.py — Downstream service wrappers as LangChain tools.

The TextService agent uses two downstream services as tools:
  - shorten_urls_tool      → calls UrlShortenService.ComposeUrls
  - resolve_mentions_tool  → calls UserMentionService.ComposeUserMentions

Each tool is a plain Python function decorated with @tool.
The tool functions receive JSON-serialisable arguments and return
JSON-serialisable results so the LLM can reason about them.

The actual Thrift connections are injected at build time via closures
(same pattern as the original TextService thrift_pool.py usage).
"""

import json
import logging
from typing import List

from langchain_core.tools import tool

logger = logging.getLogger("text-agent.tools")


def make_shorten_urls_tool(url_pool, req_id_getter, carrier_getter):
    """
    Build a LangChain tool that calls UrlShortenService.ComposeUrls.

    Parameters
    ----------
    url_pool       : ThriftClientPool for UrlShortenService
    req_id_getter  : callable() → int   (returns current req_id)
    carrier_getter : callable() → dict  (returns current OpenTracing carrier)

    Returns
    -------
    A LangChain @tool function.
    """

    @tool
    def shorten_urls(urls_json: str) -> str:
        """
        Shorten a list of expanded URLs using UrlShortenService.

        Parameters
        ----------
        urls_json : JSON array string of expanded URL strings.
                    Example: '["https://example.com/page", "https://foo.org/bar"]'

        Returns
        -------
        JSON array of objects with keys 'shortened_url' and 'expanded_url'.
        Example: '[{"shortened_url": "http://short-url/Ab3Kp9mXzQ", "expanded_url": "https://example.com/page"}]'
        Returns '[]' if the input list is empty.
        """
        try:
            urls = json.loads(urls_json)
            if not urls:
                return "[]"

            req_id  = req_id_getter()
            carrier = carrier_getter()

            with url_pool.connection() as client:
                result = client.ComposeUrls(req_id, urls, carrier)

            output = [
                {"shortened_url": u.shortened_url, "expanded_url": u.expanded_url}
                for u in result
            ]
            logger.info(
                "shorten_urls tool: %d URLs shortened", len(output)
            )
            print(f"[tool:shorten_urls] {len(urls)} URLs → {output}")
            return json.dumps(output)

        except Exception as exc:
            logger.error("shorten_urls tool failed: %s", exc)
            print(f"[tool:shorten_urls] ERROR: {exc}")
            return json.dumps({"error": str(exc)})

    return shorten_urls


def make_resolve_mentions_tool(mention_pool, req_id_getter, carrier_getter):
    """
    Build a LangChain tool that calls UserMentionService.ComposeUserMentions.

    Parameters
    ----------
    mention_pool   : ThriftClientPool for UserMentionService
    req_id_getter  : callable() → int
    carrier_getter : callable() → dict

    Returns
    -------
    A LangChain @tool function.
    """

    @tool
    def resolve_mentions(usernames_json: str) -> str:
        """
        Resolve a list of @mention usernames to user IDs using UserMentionService.

        Parameters
        ----------
        usernames_json : JSON array string of username strings (without the '@').
                         Example: '["alice", "bob"]'

        Returns
        -------
        JSON array of objects with keys 'user_id' and 'username'.
        Example: '[{"user_id": 42, "username": "alice"}, {"user_id": 7, "username": "bob"}]'
        Returns '[]' if the input list is empty.
        """
        try:
            usernames = json.loads(usernames_json)
            if not usernames:
                return "[]"

            req_id  = req_id_getter()
            carrier = carrier_getter()

            with mention_pool.connection() as client:
                result = client.ComposeUserMentions(req_id, usernames, carrier)

            output = [
                {"user_id": m.user_id, "username": m.username}
                for m in result
            ]
            logger.info(
                "resolve_mentions tool: %d usernames resolved", len(output)
            )
            print(f"[tool:resolve_mentions] {usernames} → {output}")
            return json.dumps(output)

        except Exception as exc:
            logger.error("resolve_mentions tool failed: %s", exc)
            print(f"[tool:resolve_mentions] ERROR: {exc}")
            return json.dumps({"error": str(exc)})

    return resolve_mentions