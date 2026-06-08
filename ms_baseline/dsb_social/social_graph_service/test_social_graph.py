"""
Tests for SocialGraphService Python port.

Run with:
    cd social-graph-service
    PYTHONPATH=gen-py python -m pytest test_social_graph.py -v
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
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode

from .handler import SocialGraphHandler, _KEY_FOLLOWERS, _KEY_FOLLOWEES
from .client  import SocialGraphClient


# ============================================================
# Helpers
# ============================================================

def _make_handler(user_pool=None):
    """Return a SocialGraphHandler with fully mocked backends."""
    mongo_col    = MagicMock()
    redis_client = MagicMock()
    tracer       = opentracing.tracer
    user_pool    = user_pool or MagicMock()

    mongo_client = MagicMock()
    mongo_client.__getitem__ = MagicMock(
        return_value=MagicMock(__getitem__=MagicMock(return_value=mongo_col))
    )

    handler = SocialGraphHandler(
        mongo_client=mongo_client,
        mongo_db="social-graph",
        mongo_col="social-graph",
        redis_client=redis_client,
        user_service_pool=user_pool,
        tracer=tracer,
        num_workers=4,
    )
    handler._col   = mongo_col
    handler._redis = redis_client
    return handler, mongo_col, redis_client


# ============================================================
# InsertUser
# ============================================================

class TestInsertUser(unittest.TestCase):

    def test_insert_creates_document(self):
        h, mongo, redis = _make_handler()
        h.InsertUser(1, 42, {})
        mongo.update_one.assert_called_once()
        call_args = mongo.update_one.call_args
        self.assertEqual(call_args[0][0], {"user_id": 42})
        self.assertIn("$setOnInsert", call_args[0][1])
        self.assertTrue(call_args[1].get("upsert"))

    def test_insert_document_has_empty_lists(self):
        h, mongo, redis = _make_handler()
        h.InsertUser(1, 42, {})
        doc = mongo.update_one.call_args[0][1]["$setOnInsert"]
        self.assertEqual(doc["followers"], [])
        self.assertEqual(doc["followees"], [])
        self.assertEqual(doc["user_id"],   42)

    def test_insert_mongo_error_raises_se(self):
        h, mongo, redis = _make_handler()
        mongo.update_one.side_effect = Exception("network error")
        with self.assertRaises(ServiceException) as ctx:
            h.InsertUser(1, 42, {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)


# ============================================================
# GetFollowers / GetFollowees
# ============================================================

class TestGetFollowers(unittest.TestCase):

    def test_cache_hit_returns_ids(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 1
        redis.zrange.return_value = [b"10", b"20", b"30"]

        result = h.GetFollowers(1, 5, {})
        self.assertEqual(sorted(result), [10, 20, 30])
        mongo.find_one.assert_not_called()

    def test_cache_miss_falls_through_to_mongo(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 0
        mongo.find_one.return_value = {"followers": [10, 20], "user_id": 5}

        result = h.GetFollowers(1, 5, {})
        self.assertEqual(sorted(result), [10, 20])

    def test_cache_miss_backfills_redis(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 0
        mongo.find_one.return_value = {"followers": [10, 20], "user_id": 5}

        h.GetFollowers(1, 5, {})
        redis.zadd.assert_called_once()
        zadd_call = redis.zadd.call_args[0]
        self.assertEqual(zadd_call[0], _KEY_FOLLOWERS + "5")

    def test_no_document_returns_empty(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 0
        mongo.find_one.return_value = None

        result = h.GetFollowers(1, 999, {})
        self.assertEqual(result, [])

    def test_mongo_error_raises_se(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 0
        mongo.find_one.side_effect = Exception("timeout")
        with self.assertRaises(ServiceException) as ctx:
            h.GetFollowers(1, 5, {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    def test_redis_error_falls_through(self):
        import redis as redis_lib
        h, mongo, redis_mock = _make_handler()
        redis_mock.exists.side_effect = redis_lib.RedisError("timeout")
        mongo.find_one.return_value = {"followers": [7, 8], "user_id": 5}

        result = h.GetFollowers(1, 5, {})
        self.assertEqual(sorted(result), [7, 8])


class TestGetFollowees(unittest.TestCase):

    def test_cache_hit_returns_ids(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 1
        redis.zrange.return_value = [b"3", b"7"]

        result = h.GetFollowees(1, 1, {})
        self.assertEqual(sorted(result), [3, 7])
        mongo.find_one.assert_not_called()

    def test_uses_followees_redis_key(self):
        h, mongo, redis = _make_handler()
        redis.exists.return_value = 0
        mongo.find_one.return_value = {"followees": [3, 7], "user_id": 1}

        h.GetFollowees(1, 1, {})
        # Backfill key should be followees:1
        redis.zadd.assert_called_once()
        key = redis.zadd.call_args[0][0]
        self.assertEqual(key, _KEY_FOLLOWEES + "1")


# ============================================================
# Follow
# ============================================================

class TestFollow(unittest.TestCase):

    def test_follow_writes_to_redis(self):
        h, mongo, redis = _make_handler()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__  = MagicMock(return_value=False)

        h.Follow(1, 10, 20, {})

        redis.pipeline.assert_called_once()
        pipe.zadd.assert_called()
        # Check both directions
        zadd_calls = pipe.zadd.call_args_list
        keys = [c[0][0] for c in zadd_calls]
        self.assertIn(_KEY_FOLLOWEES + "10", keys)
        self.assertIn(_KEY_FOLLOWERS + "20", keys)

    def test_follow_writes_to_mongo(self):
        h, mongo, redis = _make_handler()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe
        pipe.execute.return_value = None

        h.Follow(1, 10, 20, {})

        self.assertEqual(mongo.update_one.call_count, 2)
        calls = mongo.update_one.call_args_list
        # First call: add followee_id to user's followees
        self.assertEqual(calls[0][0][0], {"user_id": 10})
        self.assertIn("$addToSet", calls[0][0][1])
        self.assertIn("followees", calls[0][0][1]["$addToSet"])
        # Second call: add user_id to followee's followers
        self.assertEqual(calls[1][0][0], {"user_id": 20})
        self.assertIn("followers", calls[1][0][1]["$addToSet"])

    def test_follow_mongo_error_raises_se(self):
        h, mongo, redis = _make_handler()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe
        pipe.execute.return_value = None
        mongo.update_one.side_effect = Exception("write failed")

        with self.assertRaises(ServiceException) as ctx:
            h.Follow(1, 10, 20, {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_MONGODB_ERROR)

    def test_follow_redis_error_is_non_fatal(self):
        """Redis failure during Follow should not prevent MongoDB write."""
        import redis as redis_lib
        h, mongo, redis_mock = _make_handler()
        redis_mock.pipeline.side_effect = redis_lib.RedisError("pipe failed")

        # Should not raise — MongoDB write still proceeds
        h.Follow(1, 10, 20, {})
        self.assertEqual(mongo.update_one.call_count, 2)

    def test_follow_idempotent_via_addttoset(self):
        """$addToSet in MongoDB makes Follow idempotent."""
        h, mongo, redis = _make_handler()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe
        h.Follow(1, 10, 20, {})
        h.Follow(2, 10, 20, {})
        # Both calls should use $addToSet (not $push)
        for c in mongo.update_one.call_args_list:
            self.assertIn("$addToSet", c[0][1])


# ============================================================
# Unfollow
# ============================================================

class TestUnfollow(unittest.TestCase):

    def test_unfollow_removes_from_redis(self):
        h, mongo, redis = _make_handler()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe

        h.Unfollow(1, 10, 20, {})

        pipe.zrem.assert_called()
        zrem_calls = pipe.zrem.call_args_list
        keys = [c[0][0] for c in zrem_calls]
        self.assertIn(_KEY_FOLLOWEES + "10", keys)
        self.assertIn(_KEY_FOLLOWERS + "20", keys)

    def test_unfollow_removes_from_mongo(self):
        h, mongo, redis = _make_handler()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe

        h.Unfollow(1, 10, 20, {})

        calls = mongo.update_one.call_args_list
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertIn("$pull", c[0][1])

    def test_unfollow_mongo_error_raises_se(self):
        h, mongo, redis = _make_handler()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe
        mongo.update_one.side_effect = Exception("write failed")
        with self.assertRaises(ServiceException):
            h.Unfollow(1, 10, 20, {})


# ============================================================
# FollowWithUsername / UnfollowWithUsername
# ============================================================

class TestFollowWithUsername(unittest.TestCase):

    def _make_user_pool(self, alice_id=1, bob_id=2):
        user_client = MagicMock()
        user_client.GetUserId.side_effect = lambda req_id, username, carrier: (
            alice_id if username == "alice" else bob_id
        )
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=user_client)
        cm.__exit__  = MagicMock(return_value=False)
        pool = MagicMock()
        pool.connection.return_value = cm
        return pool, user_client

    def test_follow_with_username_resolves_and_follows(self):
        pool, user_client = self._make_user_pool(alice_id=1, bob_id=2)
        h, mongo, redis = _make_handler(user_pool=pool)
        pipe = MagicMock()
        redis.pipeline.return_value = pipe

        h.FollowWithUsername(1, "alice", "bob", {})

        # GetUserId called for both usernames
        calls = user_client.GetUserId.call_args_list
        usernames = [c[0][1] for c in calls]
        self.assertIn("alice", usernames)
        self.assertIn("bob",   usernames)

        # MongoDB follow called
        self.assertEqual(mongo.update_one.call_count, 2)

    def test_follow_with_username_user_not_found_raises(self):
        pool, user_client = self._make_user_pool()
        user_client.GetUserId.side_effect = ServiceException(
            errorCode=ErrorCode.SE_THRIFT_HANDLER_ERROR,
            message="User not found: ghost",
        )
        h, mongo, redis = _make_handler(user_pool=pool)

        with self.assertRaises(ServiceException):
            h.FollowWithUsername(1, "ghost", "bob", {})

    def test_unfollow_with_username_resolves_and_unfollows(self):
        pool, user_client = self._make_user_pool(alice_id=1, bob_id=2)
        h, mongo, redis = _make_handler(user_pool=pool)
        pipe = MagicMock()
        redis.pipeline.return_value = pipe

        h.UnfollowWithUsername(1, "alice", "bob", {})

        calls = mongo.update_one.call_args_list
        # Both documents get $pull
        for c in calls:
            self.assertIn("$pull", c[0][1])


# ============================================================
# Integration: follow → get followers/followees round-trip
# ============================================================

class TestFollowRoundTrip(unittest.TestCase):

    def test_follow_then_get_followers(self):
        """
        After Follow(user=1, followee=2), GetFollowers(2) should include 1
        and GetFollowees(1) should include 2.
        Uses in-memory dicts to simulate Redis + MongoDB.
        """
        # In-memory stores
        mongo_docs = {}   # user_id -> {user_id, followers, followees}
        redis_zsets = {}  # key -> {member: score}

        mongo_col = MagicMock()
        redis_mock = MagicMock()

        def mongo_update(query, update, upsert=False):
            uid = query.get("user_id")
            if uid not in mongo_docs:
                mongo_docs[uid] = {"user_id": uid, "followers": [], "followees": []}
            if "$setOnInsert" in update:
                if uid not in mongo_docs:
                    mongo_docs[uid] = update["$setOnInsert"]
            if "$addToSet" in update:
                for field, val in update["$addToSet"].items():
                    if val not in mongo_docs[uid][field]:
                        mongo_docs[uid][field].append(val)
            if "$pull" in update:
                for field, val in update["$pull"].items():
                    if val in mongo_docs[uid][field]:
                        mongo_docs[uid][field].remove(val)

        def mongo_find(query, projection=None):
            uid = query.get("user_id")
            return mongo_docs.get(uid)

        mongo_col.update_one.side_effect  = mongo_update
        mongo_col.find_one.side_effect    = mongo_find

        # Redis pipeline mock
        def make_pipe():
            pipe = MagicMock()
            zadd_ops = []
            zrem_ops = []
            pipe.zadd.side_effect = lambda k, m: zadd_ops.append((k, m))
            pipe.zrem.side_effect = lambda k, v: zrem_ops.append((k, v))

            def execute():
                for k, m in zadd_ops:
                    zset = redis_zsets.setdefault(k, {})
                    zset.update(m)
                for k, v in zrem_ops:
                    redis_zsets.get(k, {}).pop(v, None)
                zadd_ops.clear()
                zrem_ops.clear()

            pipe.execute.side_effect = execute
            return pipe

        redis_mock.pipeline.side_effect = lambda **kw: make_pipe()

        def redis_exists(key):
            return 1 if key in redis_zsets else 0

        def redis_zrange(key, start, end):
            zset = redis_zsets.get(key, {})
            sorted_members = sorted(zset.keys(), key=lambda m: zset[m])
            return [m.encode() for m in sorted_members]

        def redis_zadd(key, mapping):
            redis_zsets.setdefault(key, {}).update(mapping)

        redis_mock.exists.side_effect = redis_exists
        redis_mock.zrange.side_effect  = redis_zrange
        redis_mock.zadd.side_effect    = redis_zadd

        mongo_client = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=mongo_col))
        )

        h = SocialGraphHandler(
            mongo_client=mongo_client,
            mongo_db="social-graph",
            mongo_col="social-graph",
            redis_client=redis_mock,
            user_service_pool=MagicMock(),
            tracer=opentracing.tracer,
        )
        h._col   = mongo_col
        h._redis = redis_mock

        # Bootstrap users
        h.InsertUser(1, 1, {})
        h.InsertUser(2, 2, {})

        # Follow
        h.Follow(1, 1, 2, {})

        # GetFollowers(2) should include 1
        followers = h.GetFollowers(1, 2, {})
        self.assertIn(1, followers)

        # GetFollowees(1) should include 2
        followees = h.GetFollowees(1, 1, {})
        self.assertIn(2, followees)

        # Unfollow
        h.Unfollow(2, 1, 2, {})

        # GetFollowees(1) should now be empty
        followees_after = h.GetFollowees(2, 1, {})
        self.assertNotIn(2, followees_after)


# ============================================================
# Client unit tests
# ============================================================

def _make_client_with_mock():
    c = SocialGraphClient()
    thrift_mock    = MagicMock()
    transport_mock = MagicMock()
    transport_mock.isOpen.return_value = True
    c._client    = thrift_mock
    c._transport = transport_mock
    return c, thrift_mock


class TestSocialGraphClient(unittest.TestCase):

    def test_not_connected_raises(self):
        c = SocialGraphClient()
        with self.assertRaises(ConnectionError):
            c.get_followers(1)

    def test_insert_user_calls_thrift(self):
        c, t = _make_client_with_mock()
        c.insert_user(42)
        t.InsertUser.assert_called_once()
        self.assertEqual(t.InsertUser.call_args[0][1], 42)

    def test_follow_calls_thrift(self):
        c, t = _make_client_with_mock()
        c.follow(1, 2)
        t.Follow.assert_called_once()
        args = t.Follow.call_args[0]
        self.assertEqual(args[1], 1)
        self.assertEqual(args[2], 2)

    def test_unfollow_calls_thrift(self):
        c, t = _make_client_with_mock()
        c.unfollow(1, 2)
        t.Unfollow.assert_called_once()

    def test_get_followers_returns_list(self):
        c, t = _make_client_with_mock()
        t.GetFollowers.return_value = [10, 20, 30]
        result = c.get_followers(5)
        self.assertEqual(result, [10, 20, 30])

    def test_get_followees_returns_list(self):
        c, t = _make_client_with_mock()
        t.GetFollowees.return_value = [1, 2]
        result = c.get_followees(5)
        self.assertEqual(result, [1, 2])

    def test_follow_with_username_calls_thrift(self):
        c, t = _make_client_with_mock()
        c.follow_with_username("alice", "bob")
        t.FollowWithUsername.assert_called_once()
        args = t.FollowWithUsername.call_args[0]
        self.assertEqual(args[1], "alice")
        self.assertEqual(args[2], "bob")

    def test_unfollow_with_username_calls_thrift(self):
        c, t = _make_client_with_mock()
        c.unfollow_with_username("alice", "bob")
        t.UnfollowWithUsername.assert_called_once()

    def test_req_id_increments(self):
        c, t = _make_client_with_mock()
        t.GetFollowers.return_value = []
        c.get_followers(1)
        c.get_followers(2)
        ids = [call[0][0] for call in t.GetFollowers.call_args_list]
        self.assertEqual(ids[1], ids[0] + 1)

    def test_service_exception_propagates(self):
        c, t = _make_client_with_mock()
        t.Follow.side_effect = ServiceException(
            errorCode=ErrorCode.SE_MONGODB_ERROR, message="mongo down"
        )
        with self.assertRaises(ServiceException):
            c.follow(1, 2)

    def test_transport_exception_becomes_connection_error(self):
        c, t = _make_client_with_mock()
        t.GetFollowers.side_effect = TTransportException(message="reset")
        with self.assertRaises(ConnectionError):
            c.get_followers(1)

    def test_context_manager_closes(self):
        c = SocialGraphClient()
        with patch.object(c, "connect"), patch.object(c, "close") as mc:
            with c:
                pass
        mc.assert_called_once()

    def test_connect_retries(self):
        c = SocialGraphClient(max_retries=3, retry_delay=0)
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

    def test_aliases_work(self):
        c, t = _make_client_with_mock()
        t.InsertUser.return_value = None
        t.GetFollowers.return_value = []
        c.InsertUser(1)
        c.GetFollowers(1)
        t.InsertUser.assert_called_once()
        t.GetFollowers.assert_called_once()


# ============================================================
# Full Thrift round-trip over loopback
# ============================================================

class TestThriftRoundTrip(unittest.TestCase):

    PORT = 19993

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import SocialGraphService as SGS

        # In-memory stores
        mongo_docs  = {}
        redis_zsets = {}

        mongo_col  = MagicMock()
        redis_mock = MagicMock()

        def mongo_update(query, update, upsert=False):
            uid = query.get("user_id")
            if upsert and uid not in mongo_docs:
                mongo_docs[uid] = {"user_id": uid, "followers": [], "followees": []}
            if uid not in mongo_docs:
                return
            if "$setOnInsert" in update:
                pass  # already handled by upsert block
            if "$addToSet" in update:
                for field, val in update["$addToSet"].items():
                    if val not in mongo_docs[uid][field]:
                        mongo_docs[uid][field].append(val)
            if "$pull" in update:
                for field, val in update["$pull"].items():
                    mongo_docs[uid][field] = [
                        x for x in mongo_docs[uid][field] if x != val
                    ]

        def mongo_find(query, projection=None):
            uid = query.get("user_id")
            return mongo_docs.get(uid)

        mongo_col.update_one.side_effect = mongo_update
        mongo_col.find_one.side_effect   = mongo_find
        mongo_col.create_index = MagicMock()

        def make_pipe():
            pipe = MagicMock()
            ops = []
            pipe.zadd.side_effect = lambda k, m: ops.append(("zadd", k, m))
            pipe.zrem.side_effect = lambda k, v: ops.append(("zrem", k, v))

            def execute():
                for op in ops:
                    if op[0] == "zadd":
                        redis_zsets.setdefault(op[1], {}).update(op[2])
                    elif op[0] == "zrem":
                        redis_zsets.get(op[1], {}).pop(op[2], None)
                ops.clear()

            pipe.execute.side_effect = execute
            return pipe

        redis_mock.pipeline.side_effect = lambda **kw: make_pipe()
        redis_mock.exists.side_effect   = lambda k: 1 if k in redis_zsets else 0
        redis_mock.zrange.side_effect   = lambda k, s, e: [
            m.encode() for m in sorted(
                redis_zsets.get(k, {}),
                key=lambda x: redis_zsets[k][x]
            )
        ]
        redis_mock.zadd.side_effect = lambda k, m: redis_zsets.setdefault(k, {}).update(m)

        mongo_client = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=mongo_col))
        )

        handler = SocialGraphHandler(
            mongo_client=mongo_client,
            mongo_db="social-graph",
            mongo_col="social-graph",
            redis_client=redis_mock,
            user_service_pool=MagicMock(),
            tracer=opentracing.tracer,
        )
        handler._col   = mongo_col
        handler._redis = redis_mock

        processor = SGS.Processor(handler)
        transport = TSocket.TServerSocket(host="127.0.0.1", port=cls.PORT)
        tfactory  = TTransport.TFramedTransportFactory()
        pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

        cls.server = TServer.TThreadedServer(
            processor, transport, tfactory, pfactory, daemon=True
        )
        cls._thread = threading.Thread(target=cls.server.serve, daemon=True)
        cls._thread.start()
        time.sleep(0.5)

    def test_insert_and_follow_and_get(self):
        with SocialGraphClient("127.0.0.1", self.PORT) as c:
            c.insert_user(100)
            c.insert_user(200)
            c.follow(100, 200)

            followers_200 = c.get_followers(200)
            followees_100 = c.get_followees(100)
            self.assertIn(100, followers_200)
            self.assertIn(200, followees_100)

    def test_follow_and_unfollow(self):
        with SocialGraphClient("127.0.0.1", self.PORT) as c:
            c.insert_user(301)
            c.insert_user(302)
            c.follow(301, 302)
            c.unfollow(301, 302)

            followees = c.get_followees(301)
            self.assertNotIn(302, followees)

    def test_multiple_followers(self):
        with SocialGraphClient("127.0.0.1", self.PORT) as c:
            c.insert_user(400)
            for i in range(401, 406):
                c.insert_user(i)
                c.follow(i, 400)

            followers = c.get_followers(400)
            for i in range(401, 406):
                self.assertIn(i, followers)

    def test_get_empty_followers(self):
        with SocialGraphClient("127.0.0.1", self.PORT) as c:
            c.insert_user(500)
            followers = c.get_followers(500)
            self.assertEqual(followers, [])

    def test_concurrent_follows(self):
        errors = []
        lock   = threading.Lock()

        def run(user_id, followee_id):
            try:
                with SocialGraphClient("127.0.0.1", self.PORT) as c:
                    c.insert_user(user_id)
                    c.follow(user_id, 600)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        base = 601
        threads = [
            threading.Thread(target=run, args=(base + i, 600))
            for i in range(10)
        ]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [], f"Concurrent errors: {errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
