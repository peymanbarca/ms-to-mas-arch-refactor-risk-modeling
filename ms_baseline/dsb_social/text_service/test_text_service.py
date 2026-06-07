"""
Tests for TextService Python port.

Run with:
    cd text-service
    PYTHONPATH=gen-py python -m pytest test_text_service.py -v
"""

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call
from concurrent.futures import Future

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from thrift.transport.TTransport import TTransportException
from gen_py.social_network.ttypes import (
    Url, UserMention, TextServiceReturn, ServiceException, ErrorCode,
)

from .text_parser import extract_urls, extract_usernames, parse, replace_urls
from .handler     import TextHandler
from .client      import TextServiceClient


# ============================================================
# text_parser.py unit tests
# ============================================================

class TestExtractUrls(unittest.TestCase):

    def test_http_url(self):
        urls = extract_urls("visit http://example.com today")
        self.assertEqual(urls, ["http://example.com"])

    def test_https_url(self):
        urls = extract_urls("go to https://example.com/path")
        self.assertEqual(urls, ["https://example.com/path"])

    def test_multiple_urls(self):
        text = "see https://a.com and http://b.org/page"
        urls = extract_urls(text)
        self.assertIn("https://a.com", urls)
        self.assertIn("http://b.org/page", urls)
        self.assertEqual(len(urls), 2)

    def test_url_with_query_string(self):
        urls = extract_urls("go to https://example.com/path?q=1&x=2")
        self.assertEqual(len(urls), 1)
        self.assertIn("https://example.com/path", urls[0])

    def test_no_urls(self):
        self.assertEqual(extract_urls("hello world @alice"), [])

    def test_url_at_start(self):
        urls = extract_urls("https://example.com is great")
        self.assertEqual(urls[0], "https://example.com")

    def test_url_at_end(self):
        urls = extract_urls("visit https://example.com")
        self.assertEqual(urls[-1], "https://example.com")

    def test_duplicate_urls_preserved(self):
        text = "https://a.com and https://a.com again"
        urls = extract_urls(text)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], urls[1])

    def test_non_url_not_extracted(self):
        urls = extract_urls("ftp://old.example.com")
        self.assertEqual(urls, [])

    def test_empty_string(self):
        self.assertEqual(extract_urls(""), [])


class TestExtractUsernames(unittest.TestCase):

    def test_single_mention(self):
        names = extract_usernames("hello @alice")
        self.assertEqual(names, ["alice"])

    def test_multiple_mentions(self):
        names = extract_usernames("@alice and @bob meet @charlie")
        self.assertEqual(sorted(names), ["alice", "bob", "charlie"])

    def test_mention_with_underscore(self):
        names = extract_usernames("hello @alice_smith")
        self.assertEqual(names, ["alice_smith"])

    def test_mention_with_hyphen(self):
        names = extract_usernames("hi @alice-jones")
        self.assertEqual(names, ["alice-jones"])

    def test_mention_with_numbers(self):
        names = extract_usernames("hi @user123")
        self.assertEqual(names, ["user123"])

    def test_no_mentions(self):
        self.assertEqual(extract_usernames("no mentions here"), [])

    def test_duplicate_mentions_preserved(self):
        names = extract_usernames("@alice and @alice again")
        self.assertEqual(names.count("alice"), 2)

    def test_email_not_extracted_as_mention(self):
        # email address: @domain part only matches as mention of "domain"
        # The C++ regex would match @example in user@example.com
        # We match the same behaviour: @example is extracted
        names = extract_usernames("user@example.com")
        self.assertIn("example", names)

    def test_empty_string(self):
        self.assertEqual(extract_usernames(""), [])

    def test_at_sign_alone(self):
        self.assertEqual(extract_usernames("@ alone"), [])


class TestParse(unittest.TestCase):

    def test_mixed_text(self):
        text = "Hello @alice, check https://example.com and @bob"
        result = parse(text)
        self.assertIn("https://example.com", result.urls)
        self.assertIn("alice", result.usernames)
        self.assertIn("bob",   result.usernames)

    def test_empty_text(self):
        result = parse("")
        self.assertEqual(result.urls,      [])
        self.assertEqual(result.usernames, [])

    def test_only_urls(self):
        result = parse("visit https://a.com and https://b.org")
        self.assertEqual(len(result.urls), 2)
        self.assertEqual(result.usernames, [])

    def test_only_mentions(self):
        result = parse("@alice and @bob")
        self.assertEqual(result.urls,            [])
        self.assertEqual(sorted(result.usernames), ["alice", "bob"])


class TestReplaceUrls(unittest.TestCase):

    def test_single_replacement(self):
        text     = "visit https://example.com/long"
        url_map  = {"https://example.com/long": "http://short/Ab1"}
        result   = replace_urls(text, url_map)
        self.assertIn("http://short/Ab1", result)
        self.assertNotIn("https://example.com/long", result)

    def test_multiple_replacements(self):
        text    = "see https://a.com and http://b.org/page"
        url_map = {
            "https://a.com":       "http://short/X1",
            "http://b.org/page":   "http://short/Y2",
        }
        result = replace_urls(text, url_map)
        self.assertIn("http://short/X1", result)
        self.assertIn("http://short/Y2", result)

    def test_unknown_url_not_replaced(self):
        text   = "visit https://example.com/long"
        result = replace_urls(text, {})
        self.assertEqual(result, text)

    def test_non_url_text_preserved(self):
        text    = "Hello @alice, visit https://example.com"
        url_map = {"https://example.com": "http://short/X"}
        result  = replace_urls(text, url_map)
        self.assertIn("Hello @alice", result)
        self.assertIn("http://short/X", result)

    def test_duplicate_url_both_replaced(self):
        text    = "https://a.com and https://a.com"
        url_map = {"https://a.com": "http://s/X"}
        result  = replace_urls(text, url_map)
        self.assertEqual(result.count("http://s/X"), 2)

    def test_empty_text(self):
        self.assertEqual(replace_urls("", {"https://a.com": "http://s/X"}), "")

    def test_empty_map(self):
        text = "no urls here"
        self.assertEqual(replace_urls(text, {}), text)


# ============================================================
# TextHandler unit tests (mocked downstream services)
# ============================================================

def _make_handler():
    """Return a TextHandler with mocked ThriftClientPools."""
    url_pool     = MagicMock()
    mention_pool = MagicMock()
    tracer       = opentracing.tracer

    handler = TextHandler(url_pool, mention_pool, tracer)
    handler._url_pool     = url_pool
    handler._mention_pool = mention_pool
    return handler, url_pool, mention_pool


def _make_url_client_ctx(results):
    """Build a context manager mock that returns results from ComposeUrls."""
    client = MagicMock()
    client.ComposeUrls.return_value = results
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__  = MagicMock(return_value=False)
    return cm, client


def _make_mention_client_ctx(results):
    """Build a context manager mock for ComposeUserMentions."""
    client = MagicMock()
    client.ComposeUserMentions.return_value = results
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__  = MagicMock(return_value=False)
    return cm, client


class TestTextHandler(unittest.TestCase):

    def _setup(self, url_results=None, mention_results=None):
        h, url_pool, mention_pool = _make_handler()
        url_cm,     url_client     = _make_url_client_ctx(url_results or [])
        mention_cm, mention_client = _make_mention_client_ctx(mention_results or [])
        url_pool.connection.return_value     = url_cm
        mention_pool.connection.return_value = mention_cm
        return h, url_client, mention_client

    # --- basic text with no URLs or mentions ---

    def test_plain_text_unchanged(self):
        h, _, _ = self._setup()
        result = h.ComposeText(1, "hello world", {})
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.urls,          [])
        self.assertEqual(result.user_mentions, [])

    # --- URLs ---

    def test_url_is_shortened_in_text(self):
        url_result = [Url(
            expanded_url="https://example.com/long",
            shortened_url="http://short-url/Ab3Kp9mXzQ",
        )]
        h, url_client, _ = self._setup(url_results=url_result)
        result = h.ComposeText(1, "visit https://example.com/long today", {})
        self.assertIn("http://short-url/Ab3Kp9mXzQ", result.text)
        self.assertNotIn("https://example.com/long", result.text)
        self.assertEqual(len(result.urls), 1)

    def test_url_client_called_with_extracted_urls(self):
        h, url_client, _ = self._setup()
        h.ComposeText(1, "see https://a.com and http://b.org", {})
        call_args = url_client.ComposeUrls.call_args[0]
        passed_urls = call_args[1]
        self.assertIn("https://a.com", passed_urls)
        self.assertIn("http://b.org",  passed_urls)

    def test_no_urls_skips_url_service(self):
        h, url_pool, _ = _make_handler()
        mention_cm, _ = _make_mention_client_ctx([])
        h._mention_pool.connection.return_value = mention_cm
        h.ComposeText(1, "hello @alice no urls", {})
        url_pool.connection.assert_not_called()

    def test_multiple_urls_all_replaced(self):
        url_results = [
            Url(expanded_url="https://a.com", shortened_url="http://s/X1"),
            Url(expanded_url="https://b.com", shortened_url="http://s/X2"),
        ]
        h, _, _ = self._setup(url_results=url_results)
        result = h.ComposeText(1, "see https://a.com and https://b.com", {})
        self.assertIn("http://s/X1", result.text)
        self.assertIn("http://s/X2", result.text)

    # --- mentions ---

    def test_mention_client_called_with_extracted_usernames(self):
        h, _, mention_client = self._setup()
        h.ComposeText(1, "hello @alice and @bob", {})
        call_args = mention_client.ComposeUserMentions.call_args[0]
        passed_names = call_args[1]
        self.assertIn("alice", passed_names)
        self.assertIn("bob",   passed_names)

    def test_no_mentions_skips_mention_service(self):
        h, url_pool, mention_pool = _make_handler()
        url_cm, _ = _make_url_client_ctx([Url("http://s/X", "https://a.com")])
        h._url_pool.connection.return_value = url_cm
        h.ComposeText(1, "visit https://a.com no mentions", {})
        mention_pool.connection.assert_not_called()

    def test_user_mentions_returned(self):
        mention_results = [
            UserMention(user_id=1, username="alice"),
            UserMention(user_id=2, username="bob"),
        ]
        h, _, _ = self._setup(mention_results=mention_results)
        result = h.ComposeText(1, "hey @alice and @bob", {})
        names = [m.username for m in result.user_mentions]
        self.assertIn("alice", names)
        self.assertIn("bob",   names)

    # --- combined ---

    def test_combined_urls_and_mentions(self):
        url_results = [Url(
            expanded_url="https://example.com",
            shortened_url="http://s/Z9",
        )]
        mention_results = [UserMention(user_id=42, username="alice")]
        h, _, _ = self._setup(url_results=url_results, mention_results=mention_results)
        result = h.ComposeText(
            1, "Hey @alice, visit https://example.com", {}
        )
        self.assertIn("http://s/Z9", result.text)
        self.assertNotIn("https://example.com", result.text)
        self.assertIn("@alice", result.text)   # mention text itself stays
        self.assertEqual(result.user_mentions[0].username, "alice")
        self.assertEqual(result.urls[0].shortened_url, "http://s/Z9")

    # --- empty text ---

    def test_empty_text(self):
        h, url_pool, mention_pool = _make_handler()
        result = h.ComposeText(1, "", {})
        self.assertEqual(result.text,          "")
        self.assertEqual(result.urls,          [])
        self.assertEqual(result.user_mentions, [])
        url_pool.connection.assert_not_called()
        mention_pool.connection.assert_not_called()

    # --- downstream errors propagate ---

    def test_url_service_exception_propagates(self):
        h, url_pool, mention_pool = _make_handler()
        url_client = MagicMock()
        url_client.ComposeUrls.side_effect = ServiceException(
            errorCode=ErrorCode.SE_MONGODB_ERROR, message="mongo down"
        )
        url_cm = MagicMock()
        url_cm.__enter__ = MagicMock(return_value=url_client)
        url_cm.__exit__  = MagicMock(return_value=False)
        url_pool.connection.return_value = url_cm

        mention_cm, _ = _make_mention_client_ctx([])
        mention_pool.connection.return_value = mention_cm

        with self.assertRaises(ServiceException) as ctx:
            h.ComposeText(1, "visit https://example.com", {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    def test_mention_service_exception_propagates(self):
        h, url_pool, mention_pool = _make_handler()
        url_cm, _ = _make_url_client_ctx([])
        url_pool.connection.return_value = url_cm

        mention_client = MagicMock()
        mention_client.ComposeUserMentions.side_effect = ServiceException(
            errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR, message="user not found"
        )
        mention_cm = MagicMock()
        mention_cm.__enter__ = MagicMock(return_value=mention_client)
        mention_cm.__exit__  = MagicMock(return_value=False)
        mention_pool.connection.return_value = mention_cm

        with self.assertRaises(ServiceException):
            h.ComposeText(1, "hello @ghost", {})

    # --- carrier headers don't crash ---

    def test_carrier_headers_do_not_crash(self):
        h, _, _ = self._setup()
        result = h.ComposeText(
            1, "plain text", {"uber-trace-id": "abc:def:0:1"}
        )
        self.assertEqual(result.text, "plain text")

    # --- parallelism: both services called even if one has nothing to do ---

    def test_both_pools_used_when_both_present(self):
        url_results = [Url("http://s/X", "https://a.com")]
        mention_results = [UserMention(1, "alice")]
        h, url_client, mention_client = self._setup(
            url_results=url_results, mention_results=mention_results
        )
        h.ComposeText(1, "hey @alice visit https://a.com", {})
        url_client.ComposeUrls.assert_called_once()
        mention_client.ComposeUserMentions.assert_called_once()


# ============================================================
# Client unit tests (mocked Thrift layer)
# ============================================================

def _make_client_with_mock():
    c = TextServiceClient()
    thrift_mock    = MagicMock()
    transport_mock = MagicMock()
    transport_mock.isOpen.return_value = True
    c._client    = thrift_mock
    c._transport = transport_mock
    return c, thrift_mock


class TestTextServiceClient(unittest.TestCase):

    def test_not_connected_raises(self):
        c = TextServiceClient()
        with self.assertRaises(ConnectionError):
            c.compose_text("hello")

    def test_returns_text_service_return(self):
        c, t = _make_client_with_mock()
        t.ComposeText.return_value = TextServiceReturn(
            text="modified", user_mentions=[], urls=[]
        )
        result = c.compose_text("hello")
        self.assertEqual(result.text, "modified")

    def test_passes_text_and_carrier(self):
        c, t = _make_client_with_mock()
        t.ComposeText.return_value = TextServiceReturn("", [], [])
        carrier = {"x-trace": "abc"}
        c.compose_text("hello world", carrier=carrier)
        call_args = t.ComposeText.call_args[0]
        self.assertEqual(call_args[1], "hello world")
        self.assertEqual(call_args[2], carrier)

    def test_req_id_increments(self):
        c, t = _make_client_with_mock()
        t.ComposeText.return_value = TextServiceReturn("", [], [])
        c.compose_text("a")
        c.compose_text("b")
        ids = [call[0][0] for call in t.ComposeText.call_args_list]
        self.assertEqual(ids[1], ids[0] + 1)

    def test_service_exception_propagates(self):
        c, t = _make_client_with_mock()
        t.ComposeText.side_effect = ServiceException(
            errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR, message="downstream fail"
        )
        with self.assertRaises(ServiceException):
            c.compose_text("hello @ghost")

    def test_transport_exception_becomes_connection_error(self):
        c, t = _make_client_with_mock()
        t.ComposeText.side_effect = TTransportException(message="reset")
        with self.assertRaises(ConnectionError):
            c.compose_text("hello")

    def test_alias_ComposeText_works(self):
        c, t = _make_client_with_mock()
        t.ComposeText.return_value = TextServiceReturn("", [], [])
        c.ComposeText("hello")
        t.ComposeText.assert_called_once()

    def test_default_carrier_is_empty_dict(self):
        c, t = _make_client_with_mock()
        t.ComposeText.return_value = TextServiceReturn("", [], [])
        c.compose_text("hello")
        self.assertEqual(t.ComposeText.call_args[0][2], {})

    def test_connect_retries(self):
        c = TextServiceClient(max_retries=3, retry_delay=0)
        count = [0]
        def fake_open():
            count[0] += 1
            raise TTransportException(message="refused")
        with patch("client.TSocket.TSocket"), \
             patch("client.TTransport.TFramedTransport") as mft:
            mft.return_value.open.side_effect = fake_open
            with self.assertRaises(ConnectionError):
                c.connect()
        self.assertEqual(count[0], 3)

    def test_context_manager_closes(self):
        c = TextServiceClient()
        with patch.object(c, "connect"), patch.object(c, "close") as mc:
            with c:
                pass
        mc.assert_called_once()


# ============================================================
# Full Thrift round-trip over loopback with mocked downstreams
# ============================================================

class TestThriftRoundTrip(unittest.TestCase):

    PORT = 19994

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import TextService as TS

        # ---- Mock downstream pools ----
        url_pool     = MagicMock()
        mention_pool = MagicMock()

        # URL pool: returns shortened version of every URL
        def make_url_cm(urls):
            client = MagicMock()
            client.ComposeUrls.side_effect = lambda req_id, urls_, carrier: [
                Url(expanded_url=u, shortened_url=f"http://short-url/{abs(hash(u)) % 10**10:010d}")
                for u in urls_
            ]
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=client)
            cm.__exit__  = MagicMock(return_value=False)
            return cm

        # Mention pool: returns fake user_id for any username
        def make_mention_cm():
            client = MagicMock()
            client.ComposeUserMentions.side_effect = (
                lambda req_id, names, carrier: [
                    UserMention(user_id=abs(hash(n)) % 10000, username=n)
                    for n in names
                ]
            )
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=client)
            cm.__exit__  = MagicMock(return_value=False)
            return cm

        url_pool.connection.side_effect     = lambda: make_url_cm([])
        mention_pool.connection.side_effect = lambda: make_mention_cm()

        handler   = TextHandler(url_pool, mention_pool, opentracing.tracer)
        processor = TS.Processor(handler)
        transport = TSocket.TServerSocket(host="127.0.0.1", port=cls.PORT)
        tfactory  = TTransport.TFramedTransportFactory()
        pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

        cls.server = TServer.TThreadedServer(
            processor, transport, tfactory, pfactory, daemon=True
        )
        cls._thread = threading.Thread(target=cls.server.serve, daemon=True)
        cls._thread.start()
        time.sleep(0.5)

    def test_plain_text(self):
        with TextServiceClient("127.0.0.1", self.PORT) as c:
            result = c.compose_text("hello world")
            self.assertEqual(result.text, "hello world")
            self.assertEqual(result.urls,          [])
            self.assertEqual(result.user_mentions, [])

    def test_text_with_url(self):
        with TextServiceClient("127.0.0.1", self.PORT) as c:
            result = c.compose_text("visit https://example.com/very/long/path")
            self.assertEqual(len(result.urls), 1)
            self.assertTrue(result.urls[0].shortened_url.startswith("http://short-url/"))
            self.assertNotIn("https://example.com/very/long/path", result.text)

    def test_text_with_mention(self):
        with TextServiceClient("127.0.0.1", self.PORT) as c:
            result = c.compose_text("hey @alice how are you")
            self.assertEqual(len(result.user_mentions), 1)
            self.assertEqual(result.user_mentions[0].username, "alice")

    def test_text_with_url_and_mention(self):
        with TextServiceClient("127.0.0.1", self.PORT) as c:
            result = c.compose_text(
                "hey @alice check out https://example.com and @bob"
            )
            self.assertEqual(len(result.urls), 1)
            self.assertEqual(len(result.user_mentions), 2)
            usernames = [m.username for m in result.user_mentions]
            self.assertIn("alice", usernames)
            self.assertIn("bob",   usernames)

    def test_multiple_urls_replaced(self):
        with TextServiceClient("127.0.0.1", self.PORT) as c:
            result = c.compose_text(
                "see https://a.com and https://b.org/page"
            )
            self.assertEqual(len(result.urls), 2)
            for url_obj in result.urls:
                self.assertNotIn(url_obj.expanded_url, result.text)
                self.assertIn(url_obj.shortened_url, result.text)

    def test_empty_text(self):
        with TextServiceClient("127.0.0.1", self.PORT) as c:
            result = c.compose_text("")
            self.assertEqual(result.text,          "")
            self.assertEqual(result.urls,          [])
            self.assertEqual(result.user_mentions, [])

    def test_concurrent_requests(self):
        results = []
        errors  = []
        lock    = threading.Lock()

        def run(i):
            try:
                with TextServiceClient("127.0.0.1", self.PORT) as c:
                    text = f"hey @user{i} visit https://example{i}.com"
                    result = c.compose_text(text)
                    with lock:
                        results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 10)
        for r in results:
            self.assertEqual(len(r.urls), 1)
            self.assertEqual(len(r.user_mentions), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
