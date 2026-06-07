"""
Tests for UserMentionService Python port.

Run with:
    cd user-mention-service
    PYTHONPATH=gen-py python -m pytest test_user_mention.py -v
"""

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from thrift.transport.TTransport import TTransportException
from ms_baseline.dsb_social.gen_py.social_network.ttypes import UserMention, ServiceException, ErrorCode

from handler import UserMentionHandler
from client  import UserMentionClient


# ============================================================
# Helpers
# ============================================================

def _make_handler():
    """Return a UserMentionHandler with fully mocked MongoDB + Redis."""
    mongo_col    = MagicMock()
    redis_client = MagicMock()
    tracer       = opentracing.tracer

    mongo_client = MagicMock()
    mongo_client.__getitem__ = MagicMock(
        return_value=MagicMock(
            __getitem__=MagicMock(return_value=mongo_col)
        )
    )

    handler = UserMentionHandler(
        mongo_client=mongo_client,
        mongo_db="user",
        mongo_col="user",
        redis_client=redis_client,
        tracer=tracer,
    )
    handler._col   = mongo_col
    handler._redis = redis_client
    return handler, mongo_col, redis_client


def _doc(username: str, user_id: int) -> dict:
    return {"username": username, "user_id": user_id}


# ============================================================
# Handler — ComposeUserMentions
# ============================================================

class TestComposeUserMentions(unittest.TestCase):

    # --- cache HIT ---

    def test_cache_hit_returns_mention(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = b"42"

        result = h.ComposeUserMentions(1, ["alice"], {})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].username, "alice")
        self.assertEqual(result[0].user_id,  42)
        mongo.find_one.assert_not_called()

    def test_cache_hit_does_not_query_mongo(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = b"99"
        h.ComposeUserMentions(1, ["bob"], {})
        mongo.find_one.assert_not_called()

    # --- cache MISS, MongoDB HIT ---

    def test_cache_miss_mongo_hit_returns_mention(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = None
        mongo.find_one.return_value = _doc("alice", 7)

        result = h.ComposeUserMentions(1, ["alice"], {})

        self.assertEqual(result[0].user_id,  7)
        self.assertEqual(result[0].username, "alice")

    def test_cache_miss_mongo_hit_backfills_cache(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = None
        mongo.find_one.return_value = _doc("alice", 7)

        h.ComposeUserMentions(1, ["alice"], {})

        redis.set.assert_called_once_with("alice", "7")

    def test_mongo_projection_used(self):
        """Handler must project only user_id + username — not passwords."""
        h, mongo, redis = _make_handler()
        redis.get.return_value = None
        mongo.find_one.return_value = _doc("alice", 7)

        h.ComposeUserMentions(1, ["alice"], {})

        _, call_kwargs = mongo.find_one.call_args
        # Second positional arg or 'projection' kwarg
        call_args_pos = mongo.find_one.call_args[0]
        projection = call_args_pos[1] if len(call_args_pos) > 1 else mongo.find_one.call_args[1].get("projection")
        # The projection dict must include user_id and username, NOT password fields
        if projection:
            self.assertIn("user_id",  projection)
            self.assertIn("username", projection)

    # --- not found ---

    def test_username_not_found_raises_service_exception(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = None
        mongo.find_one.return_value = None

        with self.assertRaises(ServiceException) as ctx:
            h.ComposeUserMentions(1, ["ghost"], {})

        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_THRIFT_HANDLER_ERROR)
        self.assertIn("ghost", ctx.exception.message)

    # --- multiple usernames ---

    def test_multiple_usernames_resolved_in_order(self):
        h, mongo, redis = _make_handler()
        users = {"alice": 1, "bob": 2, "carol": 3}

        def fake_get(key):
            return None

        def fake_find(query, *a, **kw):
            uname = query["username"]
            return _doc(uname, users[uname]) if uname in users else None

        redis.get.side_effect  = fake_get
        mongo.find_one.side_effect = fake_find

        result = h.ComposeUserMentions(1, ["carol", "alice", "bob"], {})

        self.assertEqual([m.username for m in result], ["carol", "alice", "bob"])
        self.assertEqual([m.user_id  for m in result], [3, 1, 2])

    # --- duplicate usernames in one call ---

    def test_duplicate_usernames_resolved_once(self):
        """Same username appearing twice should only hit MongoDB once."""
        h, mongo, redis = _make_handler()
        redis.get.return_value = None
        mongo.find_one.return_value = _doc("alice", 5)

        result = h.ComposeUserMentions(1, ["alice", "alice"], {})

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].user_id, result[1].user_id)
        # MongoDB should only be queried ONCE for "alice"
        self.assertEqual(mongo.find_one.call_count, 1)

    # --- empty list ---

    def test_empty_list_returns_empty(self):
        h, mongo, redis = _make_handler()
        result = h.ComposeUserMentions(1, [], {})
        self.assertEqual(result, [])
        mongo.find_one.assert_not_called()

    # --- MongoDB error ---

    def test_mongo_error_raises_service_exception(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = None
        mongo.find_one.side_effect = Exception("connection refused")

        with self.assertRaises(ServiceException) as ctx:
            h.ComposeUserMentions(1, ["alice"], {})

        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    # --- Redis error is non-fatal ---

    def test_redis_error_falls_through_to_mongo(self):
        import redis as redis_lib
        h, mongo, redis_client = _make_handler()
        redis_client.get.side_effect = redis_lib.RedisError("timeout")
        mongo.find_one.return_value  = _doc("alice", 42)

        # Should NOT raise — redis error is non-fatal
        result = h.ComposeUserMentions(1, ["alice"], {})
        self.assertEqual(result[0].user_id, 42)

    def test_redis_set_error_is_non_fatal(self):
        import redis as redis_lib
        h, mongo, redis_client = _make_handler()
        redis_client.get.return_value = None
        redis_client.set.side_effect  = redis_lib.RedisError("write failed")
        mongo.find_one.return_value   = _doc("alice", 42)

        # Should NOT raise
        result = h.ComposeUserMentions(1, ["alice"], {})
        self.assertEqual(result[0].user_id, 42)

    # --- carrier headers ---

    def test_carrier_headers_do_not_crash(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = b"10"
        result = h.ComposeUserMentions(
            1, ["alice"], {"uber-trace-id": "abc:def:0:1"}
        )
        self.assertEqual(result[0].user_id, 10)

    # --- mixed cache hit/miss ---

    def test_mixed_cache_hit_and_miss(self):
        h, mongo, redis = _make_handler()

        def fake_get(key):
            return b"1" if key == "alice" else None

        mongo.find_one.return_value = _doc("bob", 2)
        redis.get.side_effect = fake_get

        result = h.ComposeUserMentions(1, ["alice", "bob"], {})
        self.assertEqual(result[0].user_id, 1)   # from cache
        self.assertEqual(result[1].user_id, 2)   # from mongo
        mongo.find_one.assert_called_once()

    # --- large batch ---

    def test_large_batch_all_from_cache(self):
        h, mongo, redis = _make_handler()
        usernames = [f"user{i}" for i in range(100)]
        redis.get.side_effect = lambda key: str(hash(key) % 10000).encode()

        result = h.ComposeUserMentions(1, usernames, {})
        self.assertEqual(len(result), 100)
        mongo.find_one.assert_not_called()

    # --- user_id type is int ---

    def test_user_id_is_int_from_cache(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = b"12345678901234"   # large i64

        result = h.ComposeUserMentions(1, ["alice"], {})
        self.assertIsInstance(result[0].user_id, int)
        self.assertEqual(result[0].user_id, 12345678901234)

    def test_user_id_is_int_from_mongo(self):
        h, mongo, redis = _make_handler()
        redis.get.return_value = None
        mongo.find_one.return_value = _doc("alice", 99999999)

        result = h.ComposeUserMentions(1, ["alice"], {})
        self.assertIsInstance(result[0].user_id, int)
        self.assertEqual(result[0].user_id, 99999999)


# ============================================================
# Client unit tests (mocked Thrift layer)
# ============================================================

def _make_client_with_mock():
    c = UserMentionClient(host="127.0.0.1", port=9093)
    thrift_mock     = MagicMock()
    transport_mock  = MagicMock()
    transport_mock.isOpen.return_value = True
    c._client    = thrift_mock
    c._transport = transport_mock
    return c, thrift_mock


class TestUserMentionClient(unittest.TestCase):

    def test_not_connected_raises(self):
        c = UserMentionClient()
        with self.assertRaises(ConnectionError):
            c.compose_user_mentions(["alice"])

    def test_returns_mention_list(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.return_value = [
            UserMention(user_id=1, username="alice")
        ]
        result = c.compose_user_mentions(["alice"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].username, "alice")
        self.assertEqual(result[0].user_id,  1)

    def test_passes_usernames_and_carrier(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.return_value = []
        carrier = {"x-trace": "abc"}
        c.compose_user_mentions(["alice", "bob"], carrier=carrier)
        call_args = thrift.ComposeUserMentions.call_args[0]
        self.assertIn("alice", call_args[1])
        self.assertIn("bob",   call_args[1])
        self.assertEqual(call_args[2], carrier)

    def test_req_id_auto_increments(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.return_value = []
        c.compose_user_mentions(["a"])
        c.compose_user_mentions(["b"])
        ids = [call[0][0] for call in thrift.ComposeUserMentions.call_args_list]
        self.assertEqual(ids[1], ids[0] + 1)

    def test_service_exception_propagates(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.side_effect = ServiceException(
            errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
            message="User not found: ghost",
        )
        with self.assertRaises(ServiceException):
            c.compose_user_mentions(["ghost"])

    def test_transport_exception_becomes_connection_error(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.side_effect = TTransportException(
            message="broken pipe"
        )
        with self.assertRaises(ConnectionError):
            c.compose_user_mentions(["alice"])

    def test_alias_ComposeUserMentions_works(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.return_value = []
        c.ComposeUserMentions(["alice"])
        thrift.ComposeUserMentions.assert_called_once()

    def test_default_carrier_is_empty_dict(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.return_value = []
        c.compose_user_mentions(["alice"])
        carrier_arg = thrift.ComposeUserMentions.call_args[0][2]
        self.assertEqual(carrier_arg, {})

    def test_empty_list_valid(self):
        c, thrift = _make_client_with_mock()
        thrift.ComposeUserMentions.return_value = []
        result = c.compose_user_mentions([])
        self.assertEqual(result, [])

    def test_close_clears_state(self):
        c, thrift = _make_client_with_mock()
        c.close()
        self.assertIsNone(c._client)
        self.assertIsNone(c._transport)

    def test_context_manager_closes(self):
        c = UserMentionClient()
        with unittest.mock.patch.object(c, "connect"), \
             unittest.mock.patch.object(c, "close") as mock_close:
            with c:
                pass
        mock_close.assert_called_once()

    def test_connect_retries_on_failure(self):
        c = UserMentionClient(max_retries=3, retry_delay=0)
        call_count = [0]

        def fake_open():
            call_count[0] += 1
            raise TTransportException(message="refused")

        with unittest.mock.patch("client.TSocket.TSocket"), \
             unittest.mock.patch("client.TTransport.TFramedTransport") as mock_ft:
            mock_ft.return_value.open.side_effect = fake_open
            with self.assertRaises(ConnectionError):
                c.connect()

        self.assertEqual(call_count[0], 3)


# ============================================================
# Full Thrift round-trip over loopback
# ============================================================

class TestThriftRoundTrip(unittest.TestCase):

    PORT = 19996

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import UserMentionService as UMS

        # In-memory user store
        users = {
            "alice":   1001,
            "bob":     1002,
            "charlie": 1003,
        }
        store = {}   # Redis simulation

        mongo_col = MagicMock()

        def fake_get(key):
            val = store.get(key)
            return val.encode() if val else None

        def fake_set(key, value):
            store[key] = value if isinstance(value, str) else value.decode()

        def fake_find(query, *a, **kw):
            uname = query.get("username")
            uid   = users.get(uname)
            return {"username": uname, "user_id": uid} if uid else None

        redis_mock = MagicMock()
        redis_mock.get.side_effect  = fake_get
        redis_mock.set.side_effect  = fake_set
        mongo_col.find_one.side_effect = fake_find

        mongo_client = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(
                __getitem__=MagicMock(return_value=mongo_col)
            )
        )

        handler = UserMentionHandler(
            mongo_client=mongo_client,
            mongo_db="user",
            mongo_col="user",
            redis_client=redis_mock,
            tracer=opentracing.tracer,
        )
        handler._col   = mongo_col
        handler._redis = redis_mock

        processor = UMS.Processor(handler)
        transport = TSocket.TServerSocket(host="127.0.0.1", port=cls.PORT)
        tfactory  = TTransport.TFramedTransportFactory()
        pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

        cls.server = TServer.TThreadedServer(
            processor, transport, tfactory, pfactory, daemon=True
        )
        cls._thread = threading.Thread(target=cls.server.serve, daemon=True)
        cls._thread.start()
        time.sleep(0.5)

    def test_resolve_single_username(self):
        with UserMentionClient("127.0.0.1", self.PORT) as c:
            result = c.compose_user_mentions(["alice"])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].username, "alice")
            self.assertEqual(result[0].user_id,  1001)

    def test_resolve_multiple_usernames(self):
        with UserMentionClient("127.0.0.1", self.PORT) as c:
            result = c.compose_user_mentions(["alice", "bob", "charlie"])
            self.assertEqual(len(result), 3)
            by_name = {m.username: m.user_id for m in result}
            self.assertEqual(by_name["alice"],   1001)
            self.assertEqual(by_name["bob"],     1002)
            self.assertEqual(by_name["charlie"], 1003)

    def test_resolve_unknown_raises_service_exception(self):
        with UserMentionClient("127.0.0.1", self.PORT) as c:
            with self.assertRaises(ServiceException) as ctx:
                c.compose_user_mentions(["nobody"])
            self.assertEqual(
                ctx.exception.errorCode, ErrorCode.SE_THRIFT_HANDLER_ERROR
            )

    def test_resolve_duplicate_usernames(self):
        with UserMentionClient("127.0.0.1", self.PORT) as c:
            result = c.compose_user_mentions(["alice", "alice"])
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].user_id, result[1].user_id)

    def test_resolve_empty_list(self):
        with UserMentionClient("127.0.0.1", self.PORT) as c:
            result = c.compose_user_mentions([])
            self.assertEqual(result, [])

    def test_concurrent_clients(self):
        results = []
        errors  = []
        lock    = threading.Lock()

        def run(username, expected_id):
            try:
                with UserMentionClient("127.0.0.1", self.PORT) as c:
                    mentions = c.compose_user_mentions([username])
                    with lock:
                        results.append((username, mentions[0].user_id, expected_id))
            except Exception as exc:
                with lock:
                    errors.append(exc)

        pairs   = [("alice", 1001), ("bob", 1002), ("charlie", 1003)] * 5
        threads = [threading.Thread(target=run, args=p) for p in pairs]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        for username, got, expected in results:
            self.assertEqual(got, expected, f"Wrong user_id for {username}")

    def test_context_manager_closes_after_use(self):
        c = UserMentionClient("127.0.0.1", self.PORT)
        with c:
            self.assertTrue(c.is_connected())
        self.assertFalse(c.is_connected())


import unittest.mock   # needed for patch in client tests

if __name__ == "__main__":
    unittest.main(verbosity=2)
