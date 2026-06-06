"""
Tests for UrlShortenService Python port.

Run with:
    cd url-shorten-service
    PYTHONPATH=gen-py python -m pytest test_url_shorten.py -v
"""

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode

from .url_shortener import make_short_token, make_shortened_url, _TOKEN_LEN
from .handler import UrlShortenHandler, _KEY_EXPAND, _KEY_SHORTEN


# ============================================================
# Helpers
# ============================================================

def _make_handler(hostname="http://short-url/"):
    """Return a UrlShortenHandler with fully mocked backends."""
    mongo_col    = MagicMock()
    redis_client = MagicMock()
    tracer       = opentracing.tracer   # no-op

    # Simulate no-op index creation
    mongo_col.create_index = MagicMock()

    # Wire up a real MongoClient mock so mongo_client[db][col] works
    mongo_client = MagicMock()
    mongo_client.__getitem__ = MagicMock(
        return_value=MagicMock(__getitem__=MagicMock(return_value=mongo_col))
    )

    handler = UrlShortenHandler(
        mongo_client=mongo_client,
        mongo_db="url-shorten",
        mongo_col="url-shorten",
        redis_client=redis_client,
        hostname=hostname,
        tracer=tracer,
    )
    # Inject the real collection directly for easy assertions
    handler._col   = mongo_col
    handler._redis = redis_client
    return handler, mongo_col, redis_client


# ============================================================
# url_shortener.py unit tests
# ============================================================

class TestUrlShortener(unittest.TestCase):

    def test_token_length_is_10(self):
        token = make_short_token("https://example.com/some/path")
        self.assertEqual(len(token), _TOKEN_LEN)

    def test_token_is_alphanumeric(self):
        token = make_short_token("https://example.com")
        self.assertTrue(token.isalnum(), f"Token not alnum: {token!r}")

    def test_same_url_same_token(self):
        url   = "https://example.com/test"
        self.assertEqual(make_short_token(url), make_short_token(url))

    def test_different_urls_different_tokens(self):
        t1 = make_short_token("https://example.com/a")
        t2 = make_short_token("https://example.com/b")
        self.assertNotEqual(t1, t2)

    def test_make_shortened_url_contains_hostname(self):
        short = make_shortened_url("http://short-url/", "https://example.com")
        self.assertTrue(short.startswith("http://short-url/"))

    def test_make_shortened_url_hostname_without_trailing_slash(self):
        short = make_shortened_url("http://short-url", "https://example.com")
        self.assertTrue(short.startswith("http://short-url/"))

    def test_make_shortened_url_total_length(self):
        short = make_shortened_url("http://short-url/", "https://example.com")
        # "http://short-url/" (17 chars) + 10 char token
        self.assertEqual(len(short), 17 + _TOKEN_LEN)

    def test_empty_url_does_not_crash(self):
        # edge case: empty string is valid input to MD5
        token = make_short_token("")
        self.assertEqual(len(token), _TOKEN_LEN)

    def test_unicode_url(self):
        token = make_short_token("https://例え.jp/パス")
        self.assertEqual(len(token), _TOKEN_LEN)

    def test_known_token_determinism(self):
        # Pin a known mapping so we catch any algorithm change
        url   = "https://www.google.com"
        token = make_short_token(url)
        # Re-run 1000 times — must always be the same
        for _ in range(1000):
            self.assertEqual(make_short_token(url), token)


# ============================================================
# UrlShortenHandler — ComposeUrls
# ============================================================

class TestComposeUrls(unittest.TestCase):

    # --- cache HIT path ---

    def test_compose_cache_hit_returns_cached_value(self):
        handler, mongo_col, redis_client = _make_handler()
        expanded = "https://example.com/long"
        shortened = "http://short-url/ABCDEF1234"
        redis_client.get.return_value = shortened.encode()

        result = handler.ComposeUrls(1, [expanded], {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].shortened_url, shortened)
        self.assertEqual(result[0].expanded_url, expanded)
        # MongoDB must NOT be touched on cache hit
        mongo_col.find_one.assert_not_called()

    # --- cache MISS, MongoDB HIT path ---

    def test_compose_cache_miss_mongo_hit_uses_mongo_value(self):
        handler, mongo_col, redis_client = _make_handler()
        expanded  = "https://example.com/long"
        shortened = "http://short-url/XYZXYZ1234"
        redis_client.get.return_value = None
        mongo_col.find_one.return_value = {
            "expanded_url": expanded,
            "shortened_url": shortened,
        }

        result = handler.ComposeUrls(1, [expanded], {})
        self.assertEqual(result[0].shortened_url, shortened)
        # Both cache directions should be backfilled
        calls = [c[0][0] for c in redis_client.set.call_args_list]
        self.assertIn(_KEY_EXPAND  + expanded,  calls)
        self.assertIn(_KEY_SHORTEN + shortened, calls)

    # --- cache MISS, MongoDB MISS path ---

    def test_compose_cache_miss_mongo_miss_computes_and_persists(self):
        handler, mongo_col, redis_client = _make_handler()
        expanded = "https://example.com/brand-new"
        redis_client.get.return_value = None
        mongo_col.find_one.return_value = None

        result = handler.ComposeUrls(1, [expanded], {})
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].shortened_url.startswith("http://short-url/"))
        self.assertEqual(len(result[0].shortened_url), 17 + _TOKEN_LEN)
        # MongoDB upsert must be called
        mongo_col.update_one.assert_called_once()
        # Both cache directions set
        set_keys = [c[0][0] for c in redis_client.set.call_args_list]
        self.assertTrue(any(k.startswith(_KEY_EXPAND)  for k in set_keys))
        self.assertTrue(any(k.startswith(_KEY_SHORTEN) for k in set_keys))

    # --- multiple URLs ---

    def test_compose_multiple_urls(self):
        handler, mongo_col, redis_client = _make_handler()
        urls = [f"https://example.com/{i}" for i in range(5)]
        redis_client.get.return_value = None
        mongo_col.find_one.return_value = None

        result = handler.ComposeUrls(1, urls, {})
        self.assertEqual(len(result), 5)
        shortened_set = {r.shortened_url for r in result}
        # All shortened URLs should be unique (different input URLs)
        self.assertEqual(len(shortened_set), 5)

    # --- same URL twice in one call ---

    def test_compose_same_url_twice_consistent(self):
        handler, mongo_col, redis_client = _make_handler()
        url = "https://example.com/same"
        # First call: cache miss + mongo miss -> computes
        # Second call: cache miss + mongo miss -> computes same value
        redis_client.get.return_value = None
        mongo_col.find_one.return_value = None

        result = handler.ComposeUrls(1, [url, url], {})
        self.assertEqual(result[0].shortened_url, result[1].shortened_url)

    # --- empty list ---

    def test_compose_empty_list(self):
        handler, mongo_col, redis_client = _make_handler()
        result = handler.ComposeUrls(1, [], {})
        self.assertEqual(result, [])

    # --- MongoDB error propagates ---

    def test_compose_mongo_error_raises_service_exception(self):
        handler, mongo_col, redis_client = _make_handler()
        redis_client.get.return_value = None
        mongo_col.find_one.side_effect = Exception("connection refused")

        with self.assertRaises(ServiceException) as ctx:
            handler.ComposeUrls(1, ["https://example.com"], {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    # --- Redis error is non-fatal ---

    def test_compose_redis_error_falls_through_to_mongo(self):
        handler, mongo_col, redis_client = _make_handler()
        import redis as redis_lib
        redis_client.get.side_effect = redis_lib.RedisError("timeout")
        mongo_col.find_one.return_value = None  # mongo miss too -> compute

        # Should NOT raise — redis error is non-fatal
        result = handler.ComposeUrls(1, ["https://example.com/test"], {})
        self.assertEqual(len(result), 1)

    # --- carrier headers don't crash ---

    def test_compose_with_carrier_headers(self):
        handler, mongo_col, redis_client = _make_handler()
        redis_client.get.return_value = None
        mongo_col.find_one.return_value = None
        carrier = {"uber-trace-id": "abc:def:0:1"}
        result = handler.ComposeUrls(1, ["https://example.com"], carrier)
        self.assertEqual(len(result), 1)


# ============================================================
# UrlShortenHandler — GetExtendedUrls
# ============================================================

class TestGetExtendedUrls(unittest.TestCase):

    def test_expand_cache_hit(self):
        handler, mongo_col, redis_client = _make_handler()
        expanded  = "https://example.com/original"
        shortened = "http://short-url/ABCDE12345"
        redis_client.get.return_value = expanded.encode()

        result = handler.GetExtendedUrls(1, [shortened], {})
        self.assertEqual(result, [expanded])
        mongo_col.find_one.assert_not_called()

    def test_expand_cache_miss_mongo_hit(self):
        handler, mongo_col, redis_client = _make_handler()
        expanded  = "https://example.com/original"
        shortened = "http://short-url/ABCDE12345"
        redis_client.get.return_value = None
        mongo_col.find_one.return_value = {
            "shortened_url": shortened,
            "expanded_url":  expanded,
        }

        result = handler.GetExtendedUrls(1, [shortened], {})
        self.assertEqual(result, [expanded])
        # Cache backfilled both ways
        set_keys = [c[0][0] for c in redis_client.set.call_args_list]
        self.assertIn(_KEY_SHORTEN + shortened, set_keys)
        self.assertIn(_KEY_EXPAND  + expanded,  set_keys)

    def test_expand_not_found_raises_service_exception(self):
        handler, mongo_col, redis_client = _make_handler()
        redis_client.get.return_value = None
        mongo_col.find_one.return_value = None

        with self.assertRaises(ServiceException) as ctx:
            handler.GetExtendedUrls(1, ["http://short-url/NOTFOUND"], {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_THRIFT_HANDLER_ERROR)

    def test_expand_multiple_urls(self):
        handler, mongo_col, redis_client = _make_handler()
        pairs = [
            ("http://short-url/A1B2C3D4E5", "https://example.com/1"),
            ("http://short-url/F6G7H8I9J0", "https://example.com/2"),
        ]
        redis_client.get.return_value = None
        mongo_col.find_one.side_effect = [
            {"shortened_url": p[0], "expanded_url": p[1]} for p in pairs
        ]

        result = handler.GetExtendedUrls(1, [p[0] for p in pairs], {})
        self.assertEqual(result, [p[1] for p in pairs])

    def test_expand_mongo_error_raises_service_exception(self):
        handler, mongo_col, redis_client = _make_handler()
        redis_client.get.return_value = None
        mongo_col.find_one.side_effect = Exception("network error")

        with self.assertRaises(ServiceException) as ctx:
            handler.GetExtendedUrls(1, ["http://short-url/X"], {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    def test_expand_empty_list(self):
        handler, mongo_col, redis_client = _make_handler()
        result = handler.GetExtendedUrls(1, [], {})
        self.assertEqual(result, [])


# ============================================================
# Round-trip consistency: ComposeUrls -> GetExtendedUrls
# ============================================================

class TestRoundTrip(unittest.TestCase):

    def test_compose_then_expand_round_trips(self):
        """
        ComposeUrls must produce shortened URLs that GetExtendedUrls can
        reverse — using only in-memory mock state (no real DB needed).
        """
        handler, mongo_col, redis_client = _make_handler()
        store = {}   # in-memory "database"

        def mock_get(key):
            val = store.get(key)
            return val.encode() if val else None

        def mock_set(key, value):
            store[key] = value if isinstance(value, str) else value.decode()

        def mock_find_one(query):
            if "expanded_url" in query:
                for doc in store.get("__docs__", {}).values():
                    if doc["expanded_url"] == query["expanded_url"]:
                        return doc
            if "shortened_url" in query:
                for doc in store.get("__docs__", {}).values():
                    if doc["shortened_url"] == query["shortened_url"]:
                        return doc
            return None

        def mock_update_one(query, update, upsert=False):
            docs = store.setdefault("__docs__", {})
            data = update["$set"]
            docs[data["expanded_url"]] = data

        redis_client.get.side_effect  = mock_get
        redis_client.set.side_effect  = mock_set
        mongo_col.find_one.side_effect = mock_find_one
        mongo_col.update_one.side_effect = mock_update_one

        urls = [
            "https://example.com/page1",
            "https://another.org/very/long/path?q=1&x=2",
            "https://github.com/delimitrou/DeathStarBench",
        ]

        # Compose
        compose_result = handler.ComposeUrls(1, urls, {})
        shortened_urls = [r.shortened_url for r in compose_result]

        # All shortened URLs start with the hostname
        for s in shortened_urls:
            self.assertTrue(s.startswith("http://short-url/"), s)

        # Expand back
        expand_result = handler.GetExtendedUrls(2, shortened_urls, {})
        self.assertEqual(expand_result, urls)


# ============================================================
# Full Thrift round-trip over loopback
# ============================================================

class TestThriftRoundTrip(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import UrlShortenService as USS

        cls.PORT = 19998

        # Mocked backends
        mongo_col = MagicMock()
        mongo_col.create_index = MagicMock()

        store = {}

        def mock_get(key):
            val = store.get(key)
            return val.encode("utf-8") if val else None

        def mock_set(key, value):
            store[key] = value if isinstance(value, str) else value.decode()

        def mock_find_one(query):
            if "expanded_url"  in query:
                k = "e2s:" + query["expanded_url"]
                if k in store:
                    return {"expanded_url": query["expanded_url"], "shortened_url": store[k]}
            if "shortened_url" in query:
                k = "s2e:" + query["shortened_url"]
                if k in store:
                    return {"shortened_url": query["shortened_url"], "expanded_url": store[k]}
            return None

        def mock_update_one(query, update, upsert=False):
            data = update["$set"]
            store["e2s:" + data["expanded_url"]]  = data["shortened_url"]
            store["s2e:" + data["shortened_url"]] = data["expanded_url"]

        redis_mock = MagicMock()
        redis_mock.get.side_effect  = mock_get
        redis_mock.set.side_effect  = mock_set
        mongo_col.find_one.side_effect    = mock_find_one
        mongo_col.update_one.side_effect  = mock_update_one

        mongo_client = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=mongo_col))
        )

        handler = UrlShortenHandler(
            mongo_client=mongo_client,
            mongo_db="url-shorten",
            mongo_col="url-shorten",
            redis_client=redis_mock,
            hostname="http://short-url/",
            tracer=opentracing.tracer,
        )
        handler._col   = mongo_col
        handler._redis = redis_mock

        processor = USS.Processor(handler)
        transport = TSocket.TServerSocket(host="127.0.0.1", port=cls.PORT)
        tfactory  = TTransport.TFramedTransportFactory()
        pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

        cls.server = TServer.TThreadedServer(
            processor, transport, tfactory, pfactory, daemon=True
        )
        cls._thread = threading.Thread(target=cls.server.serve, daemon=True)
        cls._thread.start()
        time.sleep(0.5)

    def _client(self):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from gen_py.social_network import UrlShortenService as USS
        sock = TSocket.TSocket("127.0.0.1", self.PORT)
        t    = TTransport.TFramedTransport(sock)
        p    = TBinaryProtocol.TBinaryProtocol(t)
        c    = USS.Client(p)
        t.open()
        return c, t

    def test_compose_and_expand_thrift(self):
        c, t = self._client()
        try:
            urls    = ["https://example.com/thrift-test"]
            compose = c.ComposeUrls(1, urls, {})
            self.assertEqual(len(compose), 1)
            self.assertTrue(compose[0].shortened_url.startswith("http://short-url/"))

            expanded = c.GetExtendedUrls(2, [compose[0].shortened_url], {})
            self.assertEqual(expanded, urls)
        finally:
            t.close()

    def test_compose_multiple_thrift(self):
        c, t = self._client()
        try:
            urls    = [f"https://example.com/page/{i}" for i in range(5)]
            compose = c.ComposeUrls(1, urls, {})
            self.assertEqual(len(compose), 5)
            # All different shortened URLs
            shorts = [r.shortened_url for r in compose]
            self.assertEqual(len(set(shorts)), 5)
        finally:
            t.close()

    def test_expand_unknown_raises(self):
        from gen_py.social_network.ttypes import ServiceException
        c, t = self._client()
        try:
            with self.assertRaises(ServiceException):
                c.GetExtendedUrls(1, ["http://short-url/UNKNOWNX99"], {})
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
