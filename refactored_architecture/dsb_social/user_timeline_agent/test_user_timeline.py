"""
Tests for UserTimelineService Python port.

Run with:
    cd user-timeline-service
    PYTHONPATH=gen-py python -m pytest test_user_timeline.py -v
"""

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from thrift.transport.TTransport import TTransportException
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Post, Creator, PostType, ServiceException, ErrorCode,
)

from .handler import UserTimelineHandler, _REDIS_KEY_PREFIX
from .client  import UserTimelineClient


# ============================================================
# Helpers
# ============================================================

def _make_handler(post_pool=None):
    """Return a UserTimelineHandler with fully mocked backends."""
    mongo_col    = MagicMock()
    redis_client = MagicMock()
    tracer       = opentracing.tracer
    post_pool    = post_pool or MagicMock()

    mongo_client = MagicMock()
    mongo_client.__getitem__ = MagicMock(
        return_value=MagicMock(__getitem__=MagicMock(return_value=mongo_col))
    )

    handler = UserTimelineHandler(
        mongo_client=mongo_client,
        mongo_db="user-timeline",
        mongo_col="user-timeline",
        redis_client=redis_client,
        post_storage_pool=post_pool,
        tracer=tracer,
        num_workers=4,
    )
    handler._col       = mongo_col
    handler._redis     = redis_client
    handler._post_pool = post_pool
    return handler, mongo_col, redis_client


def _make_post(post_id: int, text: str = "test", timestamp: int = 1000) -> Post:
    return Post(
        post_id=post_id,
        creator=Creator(user_id=1, username="alice"),
        req_id=0,
        text=text,
        user_mentions=[],
        media=[],
        urls=[],
        timestamp=timestamp,
        post_type=PostType.POST,
    )


def _make_post_pool(posts_by_id: dict):
    """Build a mock PostStorageService pool that returns posts by ID."""
    client = MagicMock()

    def read_posts(req_id, post_ids, carrier):
        result = []
        for pid in post_ids:
            if pid not in posts_by_id:
                raise ServiceException(
                    errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
                    message=f"Post not found: {pid}",
                )
            result.append(posts_by_id[pid])
        return result

    client.ReadPosts.side_effect = read_posts
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__  = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = cm
    return pool, client


# ============================================================
# WriteUserTimeline
# ============================================================

class TestWriteUserTimeline(unittest.TestCase):

    def test_write_adds_to_redis(self):
        h, mongo, redis = _make_handler()
        h.WriteUserTimeline(1, post_id=42, user_id=7, timestamp=9000, carrier={})
        redis.zadd.assert_called()
        call_args = redis.zadd.call_args
        key     = call_args[0][0]
        mapping = call_args[0][1]
        self.assertEqual(key, _REDIS_KEY_PREFIX + "7")
        self.assertIn("42", mapping)
        self.assertEqual(mapping["42"], 9000)

    def test_write_pushes_to_mongo(self):
        h, mongo, redis = _make_handler()
        h.WriteUserTimeline(1, post_id=42, user_id=7, timestamp=9000, carrier={})
        mongo.update_one.assert_called()
        call_args = mongo.update_one.call_args
        self.assertEqual(call_args[0][0], {"user_id": 7})
        push = call_args[0][1]["$push"]["posts"]
        self.assertEqual(push["post_id"],   42)
        self.assertEqual(push["timestamp"], 9000)
        self.assertTrue(call_args[1].get("upsert"))

    def test_write_mongo_error_raises_se(self):
        h, mongo, redis = _make_handler()
        mongo.update_one.side_effect = Exception("network error")
        with self.assertRaises(ServiceException) as ctx:
            h.WriteUserTimeline(1, 42, 7, 9000, {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    def test_write_redis_error_is_non_fatal(self):
        import redis as redis_lib
        h, mongo, redis_client = _make_handler()
        redis_client.zadd.side_effect = redis_lib.RedisError("timeout")
        # Should not raise — MongoDB write is the durable store
        h.WriteUserTimeline(1, 42, 7, 9000, {})
        mongo.update_one.assert_called()

    def test_write_upserts_on_new_user(self):
        """First write to a new user_id should create the MongoDB document."""
        h, mongo, redis = _make_handler()
        h.WriteUserTimeline(1, 1, 99, 1000, {})
        call_args = mongo.update_one.call_args
        self.assertTrue(call_args[1].get("upsert"))

    def test_multiple_writes_different_posts(self):
        h, mongo, redis = _make_handler()
        for i in range(5):
            h.WriteUserTimeline(i, post_id=i, user_id=1, timestamp=i * 1000, carrier={})
        self.assertEqual(redis.zadd.call_count, 5)
        self.assertEqual(mongo.update_one.call_count, 5)


# ============================================================
# ReadUserTimeline — Redis cache path
# ============================================================

class TestReadUserTimelineRedis(unittest.TestCase):

    def test_read_from_redis_returns_posts(self):
        post = _make_post(42)
        pool, post_client = _make_post_pool({42: post})
        h, mongo, redis = _make_handler(post_pool=pool)

        redis.exists.return_value  = 1
        redis.zrevrange.return_value = [b"42"]

        result = h.ReadUserTimeline(1, user_id=7, start=0, stop=1, carrier={})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].post_id, 42)
        mongo.find_one.assert_not_called()

    def test_read_cache_hit_calls_post_storage(self):
        post = _make_post(10)
        pool, post_client = _make_post_pool({10: post})
        h, mongo, redis = _make_handler(post_pool=pool)

        redis.exists.return_value    = 1
        redis.zrevrange.return_value = [b"10"]

        h.ReadUserTimeline(1, 7, 0, 1, {})
        post_client.ReadPosts.assert_called_once()
        passed_ids = post_client.ReadPosts.call_args[0][1]
        self.assertIn(10, passed_ids)

    def test_read_pagination_uses_start_stop(self):
        posts = {i: _make_post(i) for i in range(20)}
        pool, post_client = _make_post_pool(posts)
        h, mongo, redis = _make_handler(post_pool=pool)

        redis.exists.return_value = 1
        # Simulate page 2: items 5-9
        redis.zrevrange.return_value = [str(i).encode() for i in range(5, 10)]

        result = h.ReadUserTimeline(1, 1, start=5, stop=10, carrier={})
        self.assertEqual(len(result), 5)
        # Verify correct start/stop passed to ZREVRANGE
        zrevrange_args = redis.zrevrange.call_args[0]
        self.assertEqual(zrevrange_args[1], 5)   # start
        self.assertEqual(zrevrange_args[2], 9)   # stop - 1 (inclusive)

    def test_read_empty_redis_returns_empty(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value    = 1
        redis.zrevrange.return_value = []
        result = h.ReadUserTimeline(1, 7, 0, 10, {})
        self.assertEqual(result, [])

    def test_read_redis_error_falls_to_mongo(self):
        import redis as redis_lib
        post = _make_post(1)
        pool, _ = _make_post_pool({1: post})
        h, mongo, redis_client = _make_handler(post_pool=pool)

        redis_client.exists.side_effect = redis_lib.RedisError("timeout")
        mongo.find_one.return_value = {
            "user_id": 7,
            "posts": [{"post_id": 1, "timestamp": 1000}],
        }

        result = h.ReadUserTimeline(1, 7, 0, 1, {})
        self.assertEqual(len(result), 1)


# ============================================================
# ReadUserTimeline — MongoDB fallback path
# ============================================================

class TestReadUserTimelineMongo(unittest.TestCase):

    def test_read_from_mongo_seeds_redis(self):
        post = _make_post(5)
        pool, _ = _make_post_pool({5: post})
        h, mongo, redis = _make_handler(post_pool=pool)

        redis.exists.return_value = 0
        mongo.find_one.return_value = {
            "user_id": 1,
            "posts": [{"post_id": 5, "timestamp": 2000}],
        }

        result = h.ReadUserTimeline(1, 1, 0, 5, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].post_id, 5)
        # Redis should be seeded
        redis.zadd.assert_called()

    def test_read_from_mongo_sorts_by_timestamp_desc(self):
        posts_by_id = {
            10: _make_post(10),
            20: _make_post(20),
            30: _make_post(30),
        }
        pool, post_client = _make_post_pool(posts_by_id)
        h, mongo, redis = _make_handler(post_pool=pool)

        redis.exists.return_value = 0
        # Unsorted in MongoDB
        mongo.find_one.return_value = {
            "user_id": 1,
            "posts": [
                {"post_id": 10, "timestamp": 1000},
                {"post_id": 30, "timestamp": 3000},
                {"post_id": 20, "timestamp": 2000},
            ],
        }

        h.ReadUserTimeline(1, 1, 0, 3, {})
        # post_client.ReadPosts should be called with [30, 20, 10] (desc ts order)
        passed_ids = post_client.ReadPosts.call_args[0][1]
        self.assertEqual(passed_ids, [30, 20, 10])

    def test_read_from_mongo_pagination(self):
        posts_by_id = {i: _make_post(i) for i in range(1, 11)}
        pool, post_client = _make_post_pool(posts_by_id)
        h, mongo, redis = _make_handler(post_pool=pool)

        redis.exists.return_value = 0
        mongo.find_one.return_value = {
            "user_id": 1,
            "posts": [{"post_id": i, "timestamp": i * 100} for i in range(1, 11)],
        }

        # Read page 1: items 2-4 (start=2, stop=5)
        h.ReadUserTimeline(1, 1, start=2, stop=5, carrier={})
        passed_ids = post_client.ReadPosts.call_args[0][1]
        self.assertEqual(len(passed_ids), 3)

    def test_read_no_document_returns_empty(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 0
        mongo.find_one.return_value = None
        result = h.ReadUserTimeline(1, 99, 0, 10, {})
        self.assertEqual(result, [])

    def test_read_mongo_error_raises_se(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 0
        mongo.find_one.side_effect = Exception("connection reset")
        with self.assertRaises(ServiceException) as ctx:
            h.ReadUserTimeline(1, 1, 0, 10, {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    def test_read_post_storage_error_raises_se(self):
        pool = MagicMock()
        client = MagicMock()
        client.ReadPosts.side_effect = ServiceException(
            errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
            message="post not found",
        )
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=client)
        cm.__exit__  = MagicMock(return_value=False)
        pool.connection.return_value = cm

        h, mongo, redis = _make_handler(post_pool=pool)
        redis.exists.return_value = 1
        redis.zrevrange.return_value = [b"999"]

        with self.assertRaises(ServiceException):
            h.ReadUserTimeline(1, 1, 0, 1, {})


# ============================================================
# Client unit tests
# ============================================================

def _make_client_with_mock():
    c = UserTimelineClient()
    thrift_mock    = MagicMock()
    transport_mock = MagicMock()
    transport_mock.isOpen.return_value = True
    c._client    = thrift_mock
    c._transport = transport_mock
    return c, thrift_mock


class TestUserTimelineClient(unittest.TestCase):

    def test_not_connected_raises(self):
        c = UserTimelineClient()
        with self.assertRaises(ConnectionError):
            c.write_user_timeline(1, 42, 1000)

    def test_write_calls_thrift(self):
        c, t = _make_client_with_mock()
        c.write_user_timeline(user_id=1, post_id=42, timestamp=9000)
        t.WriteUserTimeline.assert_called_once()
        args = t.WriteUserTimeline.call_args[0]
        self.assertEqual(args[1], 42)   # post_id
        self.assertEqual(args[2], 1)    # user_id
        self.assertEqual(args[3], 9000) # timestamp

    def test_read_calls_thrift(self):
        c, t = _make_client_with_mock()
        t.ReadUserTimeline.return_value = []
        c.read_user_timeline(user_id=1, start=0, stop=5)
        t.ReadUserTimeline.assert_called_once()
        args = t.ReadUserTimeline.call_args[0]
        self.assertEqual(args[1], 1)   # user_id
        self.assertEqual(args[2], 0)   # start
        self.assertEqual(args[3], 5)   # stop

    def test_read_returns_posts(self):
        c, t = _make_client_with_mock()
        t.ReadUserTimeline.return_value = [_make_post(1), _make_post(2)]
        posts = c.read_user_timeline(1, 0, 2)
        self.assertEqual(len(posts), 2)

    def test_req_id_increments(self):
        c, t = _make_client_with_mock()
        t.ReadUserTimeline.return_value = []
        c.read_user_timeline(1, 0, 5)
        c.read_user_timeline(1, 0, 5)
        ids = [call[0][0] for call in t.ReadUserTimeline.call_args_list]
        self.assertEqual(ids[1], ids[0] + 1)

    def test_service_exception_propagates(self):
        c, t = _make_client_with_mock()
        t.ReadUserTimeline.side_effect = ServiceException(
            errorCode=ErrorCode.SE_MONGODB_ERROR, message="mongo down"
        )
        with self.assertRaises(ServiceException):
            c.read_user_timeline(1, 0, 10)

    def test_transport_exception_becomes_connection_error(self):
        c, t = _make_client_with_mock()
        t.WriteUserTimeline.side_effect = TTransportException(message="reset")
        with self.assertRaises(ConnectionError):
            c.write_user_timeline(1, 42, 1000)

    def test_aliases_work(self):
        c, t = _make_client_with_mock()
        t.ReadUserTimeline.return_value = []
        c.ReadUserTimeline(1, 0, 5)
        t.ReadUserTimeline.assert_called_once()

    def test_default_carrier_is_empty_dict(self):
        c, t = _make_client_with_mock()
        t.ReadUserTimeline.return_value = []
        c.read_user_timeline(1, 0, 5)
        carrier_arg = t.ReadUserTimeline.call_args[0][4]
        self.assertEqual(carrier_arg, {})

    def test_connect_retries(self):
        c = UserTimelineClient(max_retries=3, retry_delay=0)
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
        c = UserTimelineClient()
        with patch.object(c, "connect"), patch.object(c, "close") as mc:
            with c:
                pass
        mc.assert_called_once()


# ============================================================
# Full Thrift round-trip over loopback
# ============================================================

class TestThriftRoundTrip(unittest.TestCase):

    PORT = 19992

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import UserTimelineService as UTS

        # In-memory stores
        mongo_docs  = {}   # user_id -> {"user_id": int, "posts": [...]}
        redis_zsets = {}   # key -> {member: score}

        mongo_col  = MagicMock()
        redis_mock = MagicMock()

        def mongo_update(query, update, upsert=False):
            uid = query.get("user_id")
            if upsert and uid not in mongo_docs:
                mongo_docs[uid] = {"user_id": uid, "posts": []}
            if uid in mongo_docs and "$push" in update:
                mongo_docs[uid]["posts"].append(update["$push"]["posts"])

        def mongo_find(query, projection=None):
            uid = query.get("user_id")
            return mongo_docs.get(uid)

        mongo_col.update_one.side_effect = mongo_update
        mongo_col.find_one.side_effect   = mongo_find
        mongo_col.create_index = MagicMock()

        redis_mock.exists.side_effect = lambda k: 1 if k in redis_zsets else 0
        redis_mock.zadd.side_effect   = lambda k, m: redis_zsets.setdefault(k, {}).update(m)
        redis_mock.zrevrange.side_effect = lambda k, s, e: [
            m.encode() for m in sorted(
                redis_zsets.get(k, {}),
                key=lambda x: -redis_zsets[k][x]
            )[s: None if e == -1 else e + 1]
        ]

        # PostStorageService mock: stores posts by id
        posts_store = {}

        post_client = MagicMock()
        def read_posts(req_id, post_ids, carrier):
            return [posts_store[pid] for pid in post_ids if pid in posts_store]
        post_client.ReadPosts.side_effect = read_posts
        post_cm = MagicMock()
        post_cm.__enter__ = MagicMock(return_value=post_client)
        post_cm.__exit__  = MagicMock(return_value=False)
        post_pool = MagicMock()
        post_pool.connection.return_value = post_cm

        cls.posts_store = posts_store

        mongo_client = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=mongo_col))
        )

        handler = UserTimelineHandler(
            mongo_client=mongo_client,
            mongo_db="user-timeline",
            mongo_col="user-timeline",
            redis_client=redis_mock,
            post_storage_pool=post_pool,
            tracer=opentracing.tracer,
        )
        handler._col       = mongo_col
        handler._redis     = redis_mock
        handler._post_pool = post_pool

        processor = UTS.Processor(handler)
        transport = TSocket.TServerSocket(host="127.0.0.1", port=cls.PORT)
        tfactory  = TTransport.TFramedTransportFactory()
        pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

        cls.server = TServer.TThreadedServer(
            processor, transport, tfactory, pfactory, daemon=True
        )
        cls._thread = threading.Thread(target=cls.server.serve, daemon=True)
        cls._thread.start()
        time.sleep(0.5)

    def test_write_then_read(self):
        post = _make_post(101, "hello world", timestamp=5000)
        self.posts_store[101] = post

        with UserTimelineClient("127.0.0.1", self.PORT) as c:
            c.write_user_timeline(user_id=10, post_id=101, timestamp=5000)
            posts = c.read_user_timeline(user_id=10, start=0, stop=5)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].post_id, 101)
        self.assertEqual(posts[0].text, "hello world")

    def test_multiple_writes_ordered_by_timestamp(self):
        for pid, ts in [(201, 1000), (202, 3000), (203, 2000)]:
            self.posts_store[pid] = _make_post(pid, timestamp=ts)

        with UserTimelineClient("127.0.0.1", self.PORT) as c:
            for pid, ts in [(201, 1000), (202, 3000), (203, 2000)]:
                c.write_user_timeline(user_id=20, post_id=pid, timestamp=ts)
            posts = c.read_user_timeline(user_id=20, start=0, stop=3)

        # Most recent (ts=3000) should be first
        self.assertEqual(posts[0].post_id, 202)

    def test_pagination(self):
        for i in range(10):
            self.posts_store[300 + i] = _make_post(300 + i, timestamp=(i + 1) * 100)

        with UserTimelineClient("127.0.0.1", self.PORT) as c:
            for i in range(10):
                c.write_user_timeline(30, 300 + i, (i + 1) * 100)
            page1 = c.read_user_timeline(30, start=0, stop=5)
            page2 = c.read_user_timeline(30, start=5, stop=10)

        self.assertEqual(len(page1), 5)
        self.assertEqual(len(page2), 5)
        all_ids = [p.post_id for p in page1 + page2]
        self.assertEqual(len(set(all_ids)), 10)

    def test_read_empty_timeline(self):
        with UserTimelineClient("127.0.0.1", self.PORT) as c:
            posts = c.read_user_timeline(user_id=9999, start=0, stop=10)
        self.assertEqual(posts, [])

    def test_concurrent_writes(self):
        errors = []
        lock   = threading.Lock()

        def run(user_id, post_id, ts):
            try:
                self.posts_store[post_id] = _make_post(post_id, timestamp=ts)
                with UserTimelineClient("127.0.0.1", self.PORT) as c:
                    c.write_user_timeline(user_id, post_id, ts)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(40, 400 + i, i * 100))
            for i in range(8)
        ]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [], f"Errors: {errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
