"""
UrlShortenHandler — Python port of UrlShortenHandler.h

Implements the Thrift UrlShortenService.Iface interface.

What the C++ original does
--------------------------

ComposeUrls(req_id, urls, carrier) -> list<Url>
  For each expanded URL in `urls`:
    1. Check Memcached (Redis in our port) for cached shortened form
       (cache key = "expand:<expanded_url>").
    2. If cached: use cached value, skip MongoDB.
    3. If not cached:
         a. Check MongoDB for an existing mapping document.
         b. If found: use stored shortened_url, backfill both cache directions.
         c. If not found: compute shortened_url (MD5 -> base62 -> 10 chars),
            upsert into MongoDB, populate both cache directions.
    4. Return list<Url> with (shortened_url, expanded_url) pairs.

GetExtendedUrls(req_id, shortened_urls, carrier) -> list<string>
  For each shortened_url:
    1. Check Redis for cached expanded form
       (cache key = "shorten:<shortened_url>").
    2. If cached: use cached value.
    3. If not cached: query MongoDB by shortened_url, backfill cache.
    4. If not in MongoDB: raise ServiceException.
    5. Return list of expanded_url strings.

Redis cache keys (replacing original Memcached):
  "expand:<expanded_url>"   -> shortened_url
  "shorten:<shortened_url>" -> expanded_url

MongoDB document schema:
  { "expanded_url": str, "shortened_url": str }
  Unique indices on both fields.
"""

import logging

import opentracing
from opentracing.ext import tags as ot_tags
from opentracing.propagation import Format
from pymongo import MongoClient
import redis

from ms_baseline.dsb_social.gen_py.social_network import UrlShortenService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Url, ServiceException, ErrorCode
from .url_shortener import make_shortened_url

logger = logging.getLogger("url-shorten-service")

_CACHE_TTL = 0          # 0 = no expiry (matches original Memcached behaviour)
_KEY_EXPAND  = "expand:"    # expanded_url  -> shortened_url
_KEY_SHORTEN = "shorten:"   # shortened_url -> expanded_url


class UrlShortenHandler(UrlShortenService.Iface):

    def __init__(
        self,
        mongo_client: MongoClient,
        mongo_db: str,
        mongo_col: str,
        redis_client: redis.Redis,
        hostname: str,
        tracer: opentracing.Tracer,
    ):
        self._col    = mongo_client[mongo_db][mongo_col]
        self._redis  = redis_client
        self._host   = hostname
        self._tracer = tracer

        # Unique indices — mirror C++ MongoDB initialisation in the handler ctor
        self._col.create_index("expanded_url",  unique=True, background=True)
        self._col.create_index("shortened_url", unique=True, background=True)

    # ------------------------------------------------------------------
    # Thrift interface
    # ------------------------------------------------------------------

    def ComposeUrls(self, req_id: int, urls: list, carrier: dict) -> list:
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "ComposeUrls",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "url_count": len(urls),
            },
        ) as scope:
            span = scope.span
            result = []
            for expanded_url in urls:
                shortened = self._shorten_one(expanded_url, req_id, span)
                result.append(Url(shortened_url=shortened, expanded_url=expanded_url))
            logger.debug("ComposeUrls req_id=%d -> %d URLs", req_id, len(result))
            return result

    def GetExtendedUrls(
        self, req_id: int, shortened_urls: list, carrier: dict
    ) -> list:
        parent_ctx = self._extract_ctx(carrier)
        with self._tracer.start_active_span(
            "GetExtendedUrls",
            child_of=parent_ctx,
            tags={
                ot_tags.SPAN_KIND: ot_tags.SPAN_KIND_RPC_SERVER,
                "req_id": req_id,
                "url_count": len(shortened_urls),
            },
        ) as scope:
            span = scope.span
            result = []
            for shortened_url in shortened_urls:
                expanded = self._expand_one(shortened_url, req_id, span)
                result.append(expanded)
            logger.debug(
                "GetExtendedUrls req_id=%d -> %d URLs", req_id, len(result)
            )
            return result

    # ------------------------------------------------------------------
    # Shorten one URL
    # ------------------------------------------------------------------

    def _shorten_one(self, expanded_url: str, req_id: int, span) -> str:
        # 1. Redis cache hit
        cached = self._cache_get(_KEY_EXPAND + expanded_url, span)
        if cached is not None:
            logger.debug("ComposeUrls cache HIT expanded=%s", expanded_url)
            return cached

        # 2. MongoDB lookup (mapping may already exist)
        doc = self._mongo_find("expanded_url", expanded_url, span)
        if doc is not None:
            shortened = doc["shortened_url"]
            self._cache_set(_KEY_EXPAND  + expanded_url, shortened, span)
            self._cache_set(_KEY_SHORTEN + shortened, expanded_url, span)
            return shortened

        # 3. Compute and persist
        shortened = make_shortened_url(self._host, expanded_url)
        self._mongo_upsert(expanded_url, shortened, span)
        self._cache_set(_KEY_EXPAND  + expanded_url, shortened, span)
        self._cache_set(_KEY_SHORTEN + shortened, expanded_url, span)
        logger.debug("ComposeUrls COMPUTED %s -> %s", expanded_url, shortened)
        return shortened

    # ------------------------------------------------------------------
    # Expand one shortened URL
    # ------------------------------------------------------------------

    def _expand_one(self, shortened_url: str, req_id: int, span) -> str:
        # 1. Redis cache hit
        cached = self._cache_get(_KEY_SHORTEN + shortened_url, span)
        if cached is not None:
            logger.debug("GetExtendedUrls cache HIT shortened=%s", shortened_url)
            return cached

        # 2. MongoDB lookup
        doc = self._mongo_find("shortened_url", shortened_url, span)
        if doc is None:
            msg = f"shortened_url not found: {shortened_url}"
            span.set_tag("error", True)
            raise ServiceException(
                errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                message=msg,
            )

        expanded = doc["expanded_url"]
        self._cache_set(_KEY_SHORTEN + shortened_url, expanded, span)
        self._cache_set(_KEY_EXPAND  + expanded, shortened_url, span)
        return expanded

    # ------------------------------------------------------------------
    # MongoDB helpers
    # ------------------------------------------------------------------

    def _mongo_find(self, field: str, value: str, span) -> dict | None:
        try:
            return self._col.find_one({field: value})
        except Exception as exc:
            logger.error("MongoDB find %s=%s failed: %s", field, value, exc)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB read failed: {exc}",
            )

    def _mongo_upsert(self, expanded_url: str, shortened_url: str, span) -> None:
        try:
            self._col.update_one(
                {"expanded_url": expanded_url},
                {"$set": {
                    "expanded_url":  expanded_url,
                    "shortened_url": shortened_url,
                }},
                upsert=True,
            )
        except Exception as exc:
            logger.error("MongoDB upsert failed: %s", exc)
            span.log_kv({"event": "mongo_error", "error": str(exc)})
            raise ServiceException(
                errorCode=ErrorCode.SE_MONGODB_ERROR,
                message=f"MongoDB write failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Redis helpers
    # ------------------------------------------------------------------

    def _cache_get(self, key: str, span) -> str | None:
        try:
            val = self._redis.get(key)
            return val.decode("utf-8") if val is not None else None
        except redis.RedisError as exc:
            logger.warning("Redis GET key=%s failed: %s", key, exc)
            span.log_kv({"event": "redis_get_error", "key": key, "error": str(exc)})
            return None     # non-fatal; fall through to MongoDB

    def _cache_set(self, key: str, value: str, span) -> None:
        try:
            if _CACHE_TTL > 0:
                self._redis.setex(key, _CACHE_TTL, value)
            else:
                self._redis.set(key, value)
        except redis.RedisError as exc:
            logger.warning("Redis SET key=%s failed: %s", key, exc)
            span.log_kv({"event": "redis_set_error", "key": key, "error": str(exc)})
            # Non-fatal — data is already in MongoDB

    # ------------------------------------------------------------------
    # Tracing helper
    # ------------------------------------------------------------------

    def _extract_ctx(self, carrier: dict):
        try:
            return self._tracer.extract(Format.TEXT_MAP, carrier)
        except Exception:
            return None
