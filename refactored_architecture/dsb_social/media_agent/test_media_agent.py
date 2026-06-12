"""
Tests for Media LangGraph Agent.

Run with:
    cd media-agent
    PYTHONPATH=gen-py python -m pytest test_media_agent.py -v

All LLM calls are mocked — Ollama is NOT required.
"""

import sys
import os
import asyncio
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Media, ServiceException, ErrorCode

from agent import (
    MediaAgentState,
    _parse_json,
    _is_valid_type,
    make_check_cache_node,
    make_reason_validate_media_node,
    make_validate_output_node,
    make_persist_node,
    build_media_agent,
)
from handler import MediaHandler


# ============================================================
# Helpers
# ============================================================

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _state(**overrides) -> MediaAgentState:
    base: MediaAgentState = {
        "req_id":              1,
        "media_ids":           [100, 101],
        "media_types":         ["photo", "video"],
        "cached_results":      [],
        "uncached_ids":        [],
        "uncached_types":      [],
        "llm_validated_types": None,
        "validated_types":     [],
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
        "fallback_used":       False,
    }
    base.update(overrides)
    return base


def _mock_redis(store: dict = None):
    store = store or {}
    r = MagicMock()
    r.get.side_effect = lambda k: store.get(k, None)
    r.set.side_effect = lambda k, v: store.update(
        {k: v if isinstance(v, bytes) else v.encode() if isinstance(v, str) else v}
    )
    return r, store


def _mock_mongo():
    col   = MagicMock()
    store = {}

    def update_one(query, update, upsert=False):
        data = update.get("$set", {})
        mid  = data.get("media_id")
        if mid is not None:
            store[mid] = data

    col.update_one.side_effect = update_one
    col.create_index = MagicMock()
    return col, store


def _make_llm_response(validated_items: list, in_tok=80, out_tok=30):
    """Build a mock LLM response for reason_validate_media."""
    resp = MagicMock()
    resp.text.return_value = (
        '{"validated_items": ['
        + ", ".join(
            f'{{"media_id": {item["media_id"]}, "validated_type": "{item["validated_type"]}"}}'
            for item in validated_items
        )
        + "]}"
    )
    resp.usage_metadata = {
        "input_tokens":  in_tok,
        "output_tokens": out_tok,
        "total_tokens":  in_tok + out_tok,
    }
    return resp


# ============================================================
# Helpers unit tests
# ============================================================

class TestHelpers(unittest.TestCase):

    def test_parse_json_valid(self):
        result = _parse_json('{"validated_items": [{"media_id": 1, "validated_type": "photo"}]}')
        self.assertIn("validated_items", result)

    def test_parse_json_with_surrounding_text(self):
        result = _parse_json('Here is my answer: {"key": "val"} done')
        self.assertEqual(result, {"key": "val"})

    def test_parse_json_invalid(self):
        self.assertIsNone(_parse_json("not json"))

    def test_is_valid_type_true(self):
        for t in ["photo", "video", "audio", "document", "jpeg", "mp4"]:
            self.assertTrue(_is_valid_type(t), f"Expected valid: {t!r}")

    def test_is_valid_type_false(self):
        for t in ["", None, 123, "   "]:
            self.assertFalse(_is_valid_type(t), f"Expected invalid: {t!r}")


# ============================================================
# Node: check_cache
# ============================================================

class TestCheckCache(unittest.TestCase):

    def test_all_cache_miss(self):
        redis_mock, _ = _mock_redis({})
        node  = make_check_cache_node(redis_mock)
        state = _state(media_ids=[10, 20], media_types=["photo", "video"])
        out   = _run(node(state))
        self.assertEqual(out["cached_results"], [])
        self.assertEqual(out["uncached_ids"],   [10, 20])
        self.assertEqual(out["uncached_types"], ["photo", "video"])

    def test_all_cache_hit(self):
        redis_mock, _ = _mock_redis({
            "10": b"photo",
            "20": b"video",
        })
        node  = make_check_cache_node(redis_mock)
        state = _state(media_ids=[10, 20], media_types=["photo", "video"])
        out   = _run(node(state))
        self.assertEqual(len(out["cached_results"]), 2)
        self.assertEqual(out["uncached_ids"],   [])
        self.assertEqual(out["uncached_types"], [])

    def test_partial_cache_hit(self):
        redis_mock, _ = _mock_redis({"10": b"photo"})
        node  = make_check_cache_node(redis_mock)
        state = _state(media_ids=[10, 20], media_types=["photo", "video"])
        out   = _run(node(state))
        self.assertEqual(len(out["cached_results"]), 1)
        self.assertEqual(out["cached_results"][0]["media_id"],   10)
        self.assertEqual(out["cached_results"][0]["media_type"], "photo")
        self.assertEqual(out["uncached_ids"],   [20])
        self.assertEqual(out["uncached_types"], ["video"])

    def test_cached_type_used_from_redis(self):
        """Redis cached type overrides the input type."""
        redis_mock, _ = _mock_redis({"10": b"normalized_photo"})
        node  = make_check_cache_node(redis_mock)
        state = _state(media_ids=[10], media_types=["jpeg"])  # input is "jpeg"
        out   = _run(node(state))
        self.assertEqual(out["cached_results"][0]["media_type"], "normalized_photo")

    def test_redis_error_treated_as_miss(self):
        import redis as redis_lib
        redis_mock = MagicMock()
        redis_mock.get.side_effect = redis_lib.RedisError("timeout")
        node  = make_check_cache_node(redis_mock)
        state = _state(media_ids=[10], media_types=["photo"])
        out   = _run(node(state))
        self.assertEqual(out["uncached_ids"], [10])

    def test_empty_input(self):
        redis_mock, _ = _mock_redis()
        node  = make_check_cache_node(redis_mock)
        state = _state(media_ids=[], media_types=[])
        out   = _run(node(state))
        self.assertEqual(out["cached_results"], [])
        self.assertEqual(out["uncached_ids"],   [])


# ============================================================
# Node: reason_validate_media  (LLM mocked)
# ============================================================

class TestReasonValidateMedia(unittest.TestCase):

    def _invoke(self, state, validated_items=None, malformed=False):
        if malformed:
            resp = MagicMock()
            resp.text.return_value = "bad json"
            resp.usage_metadata    = {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
        else:
            items = validated_items or [
                {"media_id": mid, "validated_type": mtype}
                for mid, mtype in zip(state["uncached_ids"], state["uncached_types"])
            ]
            resp = _make_llm_response(items)
        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = resp
            return _run(make_reason_validate_media_node()(state))

    def test_sets_llm_validated_types(self):
        state = _state(
            uncached_ids=[10, 20], uncached_types=["jpeg", "mp4"],
            cached_results=[]
        )
        out = self._invoke(state, [
            {"media_id": 10, "validated_type": "photo"},
            {"media_id": 20, "validated_type": "video"},
        ])
        self.assertEqual(out["llm_validated_types"], ["photo", "video"])

    def test_increments_metrics(self):
        state = _state(uncached_ids=[10], uncached_types=["photo"])
        out   = self._invoke(state)
        self.assertEqual(out["total_llm_calls"],     1)
        self.assertEqual(out["total_input_tokens"],  80)
        self.assertEqual(out["total_output_tokens"], 30)

    def test_accumulates_metrics(self):
        state = _state(
            uncached_ids=[10], uncached_types=["photo"],
            total_input_tokens=100, total_output_tokens=50, total_llm_calls=3,
        )
        out = self._invoke(state)
        self.assertEqual(out["total_llm_calls"],     4)
        self.assertEqual(out["total_input_tokens"],  180)
        self.assertEqual(out["total_output_tokens"],  80)

    def test_malformed_response_sets_none(self):
        state = _state(uncached_ids=[10], uncached_types=["photo"])
        out   = self._invoke(state, malformed=True)
        self.assertIsNone(out["llm_validated_types"])

    def test_wrong_count_sets_none(self):
        """LLM returns fewer items than expected."""
        state = _state(uncached_ids=[10, 20], uncached_types=["photo", "video"])
        out   = self._invoke(state, [{"media_id": 10, "validated_type": "photo"}])
        self.assertIsNone(out["llm_validated_types"])

    def test_skips_when_all_cached(self):
        state = _state(uncached_ids=[], uncached_types=[])
        with patch("agent.llm") as mock_llm:
            out = _run(make_reason_validate_media_node()(state))
        mock_llm.invoke.assert_not_called()
        self.assertEqual(out["llm_validated_types"], [])
        self.assertEqual(out["total_llm_calls"], 0)

    def test_normalization_returned(self):
        state = _state(uncached_ids=[10], uncached_types=["jpeg"])
        out   = self._invoke(state, [{"media_id": 10, "validated_type": "photo"}])
        self.assertEqual(out["llm_validated_types"], ["photo"])


# ============================================================
# Node: validate_output
# ============================================================

class TestValidateOutput(unittest.TestCase):

    def _run_node(self, state):
        return _run(make_validate_output_node()(state))

    def test_valid_llm_types_pass(self):
        state = _state(
            uncached_ids=[10, 20], uncached_types=["jpeg", "mp4"],
            llm_validated_types=["photo", "video"],
        )
        out = self._run_node(state)
        self.assertEqual(out["validated_types"], ["photo", "video"])
        self.assertFalse(out["fallback_used"])

    def test_none_llm_types_triggers_fallback(self):
        state = _state(
            uncached_ids=[10, 20], uncached_types=["photo", "video"],
            llm_validated_types=None,
        )
        out = self._run_node(state)
        self.assertEqual(out["validated_types"], ["photo", "video"])
        self.assertTrue(out["fallback_used"])

    def test_count_mismatch_triggers_fallback(self):
        state = _state(
            uncached_ids=[10, 20], uncached_types=["photo", "video"],
            llm_validated_types=["photo"],  # only 1, expected 2
        )
        out = self._run_node(state)
        self.assertTrue(out["fallback_used"])
        self.assertEqual(out["validated_types"], ["photo", "video"])

    def test_empty_type_in_list_triggers_fallback(self):
        state = _state(
            uncached_ids=[10, 20], uncached_types=["photo", "video"],
            llm_validated_types=["photo", ""],
        )
        out = self._run_node(state)
        self.assertTrue(out["fallback_used"])

    def test_empty_uncached_skips(self):
        state = _state(uncached_ids=[], uncached_types=[], llm_validated_types=[])
        out   = self._run_node(state)
        self.assertEqual(out["validated_types"], [])
        self.assertFalse(out["fallback_used"])

    def test_fallback_preserves_original_types(self):
        original = ["photo", "video", "audio"]
        state = _state(
            uncached_ids=[1, 2, 3], uncached_types=original,
            llm_validated_types=None,
        )
        out = self._run_node(state)
        self.assertEqual(out["validated_types"], original)

    def test_single_item_valid(self):
        state = _state(
            uncached_ids=[5], uncached_types=["jpeg"],
            llm_validated_types=["photo"],
        )
        out = self._run_node(state)
        self.assertEqual(out["validated_types"], ["photo"])
        self.assertFalse(out["fallback_used"])


# ============================================================
# Node: persist
# ============================================================

class TestPersist(unittest.TestCase):

    def test_mongo_upsert_called_per_item(self):
        redis_mock, _ = _mock_redis()
        col, store    = _mock_mongo()
        state = _state(
            uncached_ids=[10, 20],
            validated_types=["photo", "video"],
        )
        _run(make_persist_node(redis_mock, col)(state))
        self.assertEqual(col.update_one.call_count, 2)

    def test_redis_set_called_per_item(self):
        redis_mock, store = _mock_redis()
        col, _            = _mock_mongo()
        state = _state(uncached_ids=[10, 20], validated_types=["photo", "video"])
        _run(make_persist_node(redis_mock, col)(state))
        set_calls = [c[0][0] for c in redis_mock.set.call_args_list]
        self.assertIn("10", set_calls)
        self.assertIn("20", set_calls)

    def test_mongo_stores_correct_types(self):
        redis_mock, _ = _mock_redis()
        col, store    = _mock_mongo()
        state = _state(uncached_ids=[10], validated_types=["photo"])
        _run(make_persist_node(redis_mock, col)(state))
        self.assertEqual(store[10]["media_type"], "photo")

    def test_redis_error_is_non_fatal(self):
        import redis as redis_lib
        redis_mock = MagicMock()
        redis_mock.set.side_effect = redis_lib.RedisError("down")
        col, _ = _mock_mongo()
        state  = _state(uncached_ids=[10], validated_types=["photo"])
        # Should not raise
        _run(make_persist_node(redis_mock, col)(state))
        col.update_one.assert_called_once()

    def test_skips_when_nothing_uncached(self):
        redis_mock, _ = _mock_redis()
        col, _        = _mock_mongo()
        state = _state(uncached_ids=[], validated_types=[])
        _run(make_persist_node(redis_mock, col)(state))
        col.update_one.assert_not_called()


# ============================================================
# Full graph  (LLM mocked)
# ============================================================

class TestFullGraph(unittest.TestCase):

    def _run_graph(
        self, media_ids, media_types,
        llm_items=None, cache_store=None, malformed=False
    ):
        redis_mock, store = _mock_redis(cache_store or {})
        col, mongo_store  = _mock_mongo()
        graph = build_media_agent(redis_mock, col)

        if malformed:
            resp = MagicMock()
            resp.text.return_value = "bad json"
            resp.usage_metadata    = {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
        else:
            items = llm_items or [
                {"media_id": mid, "validated_type": mtype}
                for mid, mtype in zip(media_ids, media_types)
            ]
            resp = _make_llm_response(items)

        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = resp
            initial = {
                "req_id":              1,
                "media_ids":           list(media_ids),
                "media_types":         list(media_types),
                "cached_results":      [],
                "uncached_ids":        [],
                "uncached_types":      [],
                "llm_validated_types": None,
                "validated_types":     [],
                "total_input_tokens":  0,
                "total_output_tokens": 0,
                "total_llm_calls":     0,
                "fallback_used":       False,
            }
            return _run(graph.ainvoke(initial)), store, mongo_store

    def test_all_cache_miss_llm_normalizes(self):
        out, redis_store, mongo_store = self._run_graph(
            [10, 20], ["jpeg", "mp4"],
            llm_items=[
                {"media_id": 10, "validated_type": "photo"},
                {"media_id": 20, "validated_type": "video"},
            ],
        )
        self.assertEqual(out["validated_types"], ["photo", "video"])
        self.assertFalse(out["fallback_used"])
        self.assertIn(10, mongo_store)
        self.assertEqual(mongo_store[10]["media_type"], "photo")

    def test_all_cache_hit_skips_llm(self):
        redis_mock, store = _mock_redis({"10": b"photo", "20": b"video"})
        col, _ = _mock_mongo()
        graph  = build_media_agent(redis_mock, col)
        with patch("agent.llm") as mock_llm:
            initial = {
                "req_id":              1,
                "media_ids":           [10, 20],
                "media_types":         ["photo", "video"],
                "cached_results":      [],
                "uncached_ids":        [],
                "uncached_types":      [],
                "llm_validated_types": None,
                "validated_types":     [],
                "total_input_tokens":  0,
                "total_output_tokens": 0,
                "total_llm_calls":     0,
                "fallback_used":       False,
            }
            out = _run(graph.ainvoke(initial))
        mock_llm.invoke.assert_not_called()
        self.assertEqual(out["total_llm_calls"], 0)
        self.assertEqual(len(out["cached_results"]), 2)

    def test_malformed_llm_uses_fallback(self):
        out, _, _ = self._run_graph([10], ["photo"], malformed=True)
        self.assertTrue(out["fallback_used"])
        self.assertEqual(out["validated_types"], ["photo"])  # original preserved

    def test_llm_metrics_tracked(self):
        out, _, _ = self._run_graph([10], ["photo"])
        self.assertEqual(out["total_llm_calls"],     1)
        self.assertEqual(out["total_input_tokens"],  80)
        self.assertEqual(out["total_output_tokens"], 30)

    def test_partial_cache_hit_llm_only_for_uncached(self):
        redis_mock, _ = _mock_redis({"10": b"photo"})
        col, _        = _mock_mongo()
        graph = build_media_agent(redis_mock, col)

        resp = _make_llm_response([{"media_id": 20, "validated_type": "video"}])
        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = resp
            initial = {
                "req_id": 1, "media_ids": [10, 20], "media_types": ["photo", "video"],
                "cached_results": [], "uncached_ids": [], "uncached_types": [],
                "llm_validated_types": None, "validated_types": [],
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_llm_calls": 0, "fallback_used": False,
            }
            out = _run(graph.ainvoke(initial))

        # LLM called once for uncached item only
        mock_llm.invoke.assert_called_once()
        self.assertEqual(out["total_llm_calls"], 1)


# ============================================================
# Handler unit tests
# ============================================================

class TestMediaHandler(unittest.TestCase):

    def _make_handler(self):
        redis_mock, redis_store = _mock_redis()
        col, mongo_store        = _mock_mongo()
        mongo_client = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=col))
        )
        h = MediaHandler(
            mongo_client=mongo_client,
            mongo_db="media", mongo_col="media",
            redis_client=redis_mock,
            tracer=opentracing.tracer,
        )
        h._col   = col
        h._redis = redis_mock
        from agent import build_media_agent
        h._graph = build_media_agent(redis_mock, col)
        return h, redis_store, mongo_store

    def _llm_ctx(self, validated_items_fn=None):
        class _Ctx:
            def __enter__(self_):
                self_.p = patch("agent.llm")
                mock = self_.p.start()
                def invoke(prompt):
                    import re
                    ids   = [int(x) for x in re.findall(r'"media_id":\s*(\d+)', prompt)]
                    types = re.findall(r'"media_type":\s*"([^"]+)"', prompt)
                    if validated_items_fn:
                        items = [validated_items_fn(mid, mt) for mid, mt in zip(ids, types)]
                    else:
                        items = [{"media_id": mid, "validated_type": mt}
                                 for mid, mt in zip(ids, types)]
                    return _make_llm_response(items)
                mock.invoke.side_effect = invoke
                return mock
            def __exit__(self_, *a):
                self_.p.stop()
        return _Ctx()

    def test_returns_media_list(self):
        h, _, _ = self._make_handler()
        with self._llm_ctx():
            result = h.ComposeMedia(1, ["photo", "video"], [10, 20], {})
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], Media)
        self.assertIsInstance(result[1], Media)

    def test_media_ids_preserved(self):
        h, _, _ = self._make_handler()
        with self._llm_ctx():
            result = h.ComposeMedia(1, ["photo"], [42], {})
        self.assertEqual(result[0].media_id, 42)

    def test_llm_normalized_type_used(self):
        h, _, _ = self._make_handler()
        with self._llm_ctx(lambda mid, mt: {"media_id": mid, "validated_type": "photo"}):
            result = h.ComposeMedia(1, ["jpeg"], [10], {})
        self.assertEqual(result[0].media_type, "photo")

    def test_order_preserved(self):
        h, _, _ = self._make_handler()
        ids   = [10, 20, 30]
        types = ["photo", "video", "audio"]
        with self._llm_ctx():
            result = h.ComposeMedia(1, types, ids, {})
        self.assertEqual([r.media_id for r in result], ids)

    def test_length_mismatch_raises_service_exception(self):
        h, _, _ = self._make_handler()
        with self.assertRaises(ServiceException) as ctx:
            h.ComposeMedia(1, ["photo", "video"], [10], {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_THRIFT_HANDLER_ERROR)

    def test_empty_input_returns_empty(self):
        h, _, _ = self._make_handler()
        result = h.ComposeMedia(1, [], [], {})
        self.assertEqual(result, [])

    def test_cache_hit_skips_llm(self):
        redis_mock, redis_store = _mock_redis({"10": b"photo"})
        col, _ = _mock_mongo()
        mongo_client = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=col))
        )
        h = MediaHandler(
            mongo_client=mongo_client,
            mongo_db="media", mongo_col="media",
            redis_client=redis_mock,
            tracer=opentracing.tracer,
        )
        h._col   = col
        h._redis = redis_mock
        from agent import build_media_agent
        h._graph = build_media_agent(redis_mock, col)
        with patch("agent.llm") as mock_llm:
            result = h.ComposeMedia(1, ["photo"], [10], {})
        mock_llm.invoke.assert_not_called()
        self.assertEqual(result[0].media_type, "photo")

    def test_carrier_does_not_crash(self):
        h, _, _ = self._make_handler()
        with self._llm_ctx():
            result = h.ComposeMedia(
                1, ["photo"], [10], {"uber-trace-id": "abc:def:0:1"}
            )
        self.assertEqual(len(result), 1)


# ============================================================
# Thrift round-trip
# ============================================================

class TestThriftRoundTrip(unittest.TestCase):

    PORT = 19988

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import MediaService as MS
        from agent import build_media_agent

        redis_mock, _ = _mock_redis()
        col, _        = _mock_mongo()
        mongo_client  = MagicMock()
        mongo_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=col))
        )
        h = MediaHandler(
            mongo_client=mongo_client,
            mongo_db="media", mongo_col="media",
            redis_client=redis_mock,
            tracer=opentracing.tracer,
        )
        h._col   = col
        h._redis = redis_mock
        h._graph = build_media_agent(redis_mock, col)

        processor = MS.Processor(h)
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
        from gen_py.social_network import MediaService as MS
        sock      = TSocket.TSocket("127.0.0.1", self.PORT)
        transport = TTransport.TFramedTransport(sock)
        protocol  = TBinaryProtocol.TBinaryProtocol(transport)
        client    = MS.Client(protocol)
        transport.open()
        return client, transport

    def _llm_patcher(self):
        p = patch("agent.llm")
        m = p.start()
        def invoke(prompt):
            import re
            ids   = [int(x) for x in re.findall(r'"media_id":\s*(\d+)', prompt)]
            types = re.findall(r'"media_type":\s*"([^"]+)"', prompt)
            items = [{"media_id": mid, "validated_type": mt}
                     for mid, mt in zip(ids, types)]
            return _make_llm_response(items)
        m.invoke.side_effect = invoke
        return p

    def test_compose_media_returns_correct_count(self):
        p = self._llm_patcher()
        try:
            client, transport = self._client()
            result = client.ComposeMedia(1, ["photo", "video"], [100, 200], {})
            self.assertEqual(len(result), 2)
        finally:
            transport.close()
            p.stop()

    def test_compose_media_ids_match(self):
        p = self._llm_patcher()
        try:
            client, transport = self._client()
            result = client.ComposeMedia(1, ["photo"], [999], {})
            self.assertEqual(result[0].media_id, 999)
        finally:
            transport.close()
            p.stop()

    def test_length_mismatch_raises(self):
        p = self._llm_patcher()
        try:
            client, transport = self._client()
            with self.assertRaises(Exception):
                client.ComposeMedia(1, ["photo", "video"], [100], {})
        finally:
            transport.close()
            p.stop()

    def test_concurrent_requests(self):
        p = self._llm_patcher()
        errors = []
        lock   = threading.Lock()
        try:
            def run(i):
                try:
                    client, transport = self._client()
                    r = client.ComposeMedia(i, ["photo"], [i * 100], {})
                    self.assertEqual(len(r), 1)
                    transport.close()
                except Exception as exc:
                    with lock:
                        errors.append(exc)
            threads = [threading.Thread(target=run, args=(i,)) for i in range(1, 6)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [], f"Errors: {errors}")
        finally:
            p.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)