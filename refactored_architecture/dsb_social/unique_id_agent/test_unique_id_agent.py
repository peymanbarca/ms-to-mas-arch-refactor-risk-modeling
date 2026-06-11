"""
Tests for UniqueId LangGraph Agent.

Run with:
    cd unique-id-agent
    PYTHONPATH=gen-py python -m pytest test_unique_id_agent.py -v

All LLM calls are mocked — Ollama is NOT required to run the tests.
"""

import sys
import os
import asyncio
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from ms_baseline.dsb_social.gen_py.social_network.ttypes import PostType, ServiceException, ErrorCode

from .snowflake import _TIMESTAMP_SHIFT, _MACHINE_ID_SHIFT, _SEQUENCE_BITS
from .snowflake_agent import AgentSnowflakeGenerator
from .agent import (
    UniqueIdAgentState,
    gather_inputs,
    reason_unique_id,
    validate_output,
    build_unique_id_agent,
    _deterministic_snowflake,
    _parse_json,
)


# ============================================================
# Helpers
# ============================================================

def _base_state(**overrides) -> UniqueIdAgentState:
    state: UniqueIdAgentState = {
        "req_id":          1,
        "post_type":       PostType.POST,
        "machine_id":      0,
        "timestamp_ms":    1_700_000_000_000,
        "sequence":        0,
        "unique_id":       None,
        "total_input_tokens":  0,
        "total_output_tokens": 0,
        "total_llm_calls":     0,
        "fallback_used":       False,
    }
    state.update(overrides)
    return state


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_llm_response(unique_id: int, in_tok: int = 50, out_tok: int = 10):
    """Build a mock LLM response object."""
    resp = MagicMock()
    resp.text.return_value = f'{{"unique_id": {unique_id}}}'
    resp.usage_metadata = {"input_tokens": in_tok, "output_tokens": out_tok, "total_tokens": in_tok + out_tok}
    return resp


# ============================================================
# _parse_json
# ============================================================

class TestParseJson(unittest.TestCase):

    def test_extracts_valid_json(self):
        result = _parse_json('Here is the answer: {"unique_id": 123456}')
        self.assertEqual(result, {"unique_id": 123456})

    def test_handles_multiline(self):
        result = _parse_json('Some text\n{\n  "unique_id": 99\n}\nmore text')
        self.assertEqual(result["unique_id"], 99)

    def test_returns_none_on_invalid(self):
        self.assertIsNone(_parse_json("no json here"))

    def test_returns_none_on_malformed(self):
        self.assertIsNone(_parse_json("{ broken json {"))


# ============================================================
# _deterministic_snowflake
# ============================================================

class TestDeterministicSnowflake(unittest.TestCase):

    def test_correct_bit_layout(self):
        ts, mid, seq = 1_700_000_000_000, 5, 3
        uid = _deterministic_snowflake(ts, mid, seq)
        self.assertEqual(uid >> _TIMESTAMP_SHIFT, ts)
        self.assertEqual((uid >> _MACHINE_ID_SHIFT) & 0x3FF, mid)
        self.assertEqual(uid & 0xFFF, seq)

    def test_positive_result(self):
        uid = _deterministic_snowflake(1_700_000_000_000, 0, 0)
        self.assertGreater(uid, 0)

    def test_zero_sequence_zero_machine(self):
        uid = _deterministic_snowflake(1_000_000, 0, 0)
        self.assertEqual(uid, 1_000_000 << _TIMESTAMP_SHIFT)


# ============================================================
# AgentSnowflakeGenerator
# ============================================================

class TestAgentSnowflakeGenerator(unittest.TestCase):

    def test_next_inputs_returns_triple(self):
        gen = AgentSnowflakeGenerator(7)
        ts, mid, seq = gen.next_inputs()
        self.assertIsInstance(ts, int)
        self.assertEqual(mid, 7)
        self.assertIsInstance(seq, int)
        self.assertGreater(ts, 0)

    def test_next_inputs_sequence_increments_within_ms(self):
        gen = AgentSnowflakeGenerator(0)
        gen._last_ms = -1
        ts1, _, seq1 = gen.next_inputs()
        # Force same millisecond
        gen._last_ms = ts1
        _, _, seq2 = gen.next_inputs()
        self.assertEqual(seq2, seq1 + 1)

    def test_next_inputs_thread_safe_no_duplicates(self):
        gen = AgentSnowflakeGenerator(0)
        results = []
        lock = threading.Lock()

        def run():
            for _ in range(200):
                triple = gen.next_inputs()
                with lock:
                    results.append(triple)

        threads = [threading.Thread(target=run) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        # (ts, mid, seq) triples must be unique
        self.assertEqual(len(results), len(set(results)))

    def test_next_inputs_clock_backwards_raises(self):
        gen = AgentSnowflakeGenerator(0)
        gen._last_ms = int(time.time() * 1000) + 9_999_999
        with self.assertRaises(RuntimeError):
            with gen._lock:
                gen._generate()

    def test_next_id_still_works(self):
        """Original next_id() must still function (backwards compatibility)."""
        gen = AgentSnowflakeGenerator(1)
        uid = gen.next_id()
        self.assertGreater(uid, 0)


# ============================================================
# Node: gather_inputs
# ============================================================

class TestGatherInputs(unittest.TestCase):

    def test_state_unchanged(self):
        """gather_inputs is a pass-through — state must be returned as-is."""
        state = _base_state(req_id=42, machine_id=3, timestamp_ms=9999, sequence=7)
        result = _run(gather_inputs(state))
        self.assertEqual(result["req_id"],       42)
        self.assertEqual(result["machine_id"],   3)
        self.assertEqual(result["timestamp_ms"], 9999)
        self.assertEqual(result["sequence"],     7)

    def test_returns_state(self):
        state = _base_state()
        result = _run(gather_inputs(state))
        self.assertIsNotNone(result)


# ============================================================
# Node: reason_unique_id  (LLM mocked)
# ============================================================

class TestReasonUniqueId(unittest.TestCase):

    def _invoke(self, state, llm_unique_id=None, malformed=False):
        """Run reason_unique_id with a mocked LLM."""
        if malformed:
            mock_resp = MagicMock()
            mock_resp.text.return_value = "no json here at all"
            mock_resp.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        else:
            mock_resp = _make_llm_response(llm_unique_id or 999999)

        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = mock_resp
            return _run(reason_unique_id(state))

    def test_sets_unique_id_from_llm(self):
        state = _base_state()
        result = self._invoke(state, llm_unique_id=123456)
        self.assertEqual(result["unique_id"], 123456)

    def test_increments_token_counts(self):
        state = _base_state()
        result = self._invoke(state, llm_unique_id=1)
        self.assertEqual(result["total_input_tokens"],  50)
        self.assertEqual(result["total_output_tokens"], 10)
        self.assertEqual(result["total_llm_calls"],     1)

    def test_accumulates_tokens_across_calls(self):
        state = _base_state(total_input_tokens=100, total_output_tokens=20, total_llm_calls=2)
        result = self._invoke(state, llm_unique_id=1)
        self.assertEqual(result["total_input_tokens"],  150)
        self.assertEqual(result["total_output_tokens"],  30)
        self.assertEqual(result["total_llm_calls"],       3)

    def test_malformed_llm_response_sets_none(self):
        state = _base_state()
        result = self._invoke(state, malformed=True)
        self.assertIsNone(result["unique_id"])

    def test_unique_id_none_on_wrong_type(self):
        mock_resp = MagicMock()
        mock_resp.text.return_value = '{"unique_id": "not_an_int"}'
        mock_resp.usage_metadata = {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = mock_resp
            state  = _base_state()
            result = _run(reason_unique_id(state))
        self.assertIsNone(result["unique_id"])


# ============================================================
# Node: validate_output
# ============================================================

class TestValidateOutput(unittest.TestCase):

    def _correct_uid(self, ts=1_700_000_000_000, mid=5, seq=3):
        return _deterministic_snowflake(ts, mid, seq)

    def test_valid_llm_result_passes(self):
        ts, mid, seq = 1_700_000_000_000, 5, 3
        uid = _deterministic_snowflake(ts, mid, seq)
        state = _base_state(timestamp_ms=ts, machine_id=mid, sequence=seq, unique_id=uid)
        result = _run(validate_output(state))
        self.assertEqual(result["unique_id"],    uid)
        self.assertFalse(result["fallback_used"])

    def test_wrong_llm_result_triggers_fallback(self):
        ts, mid, seq = 1_700_000_000_000, 5, 3
        state = _base_state(timestamp_ms=ts, machine_id=mid, sequence=seq,
                            unique_id=9999999)  # clearly wrong
        result = _run(validate_output(state))
        expected = _deterministic_snowflake(ts, mid, seq)
        self.assertEqual(result["unique_id"],   expected)
        self.assertTrue(result["fallback_used"])

    def test_none_unique_id_triggers_fallback(self):
        state = _base_state(timestamp_ms=1_700_000_000_000, machine_id=0, sequence=0,
                            unique_id=None)
        result = _run(validate_output(state))
        self.assertIsNotNone(result["unique_id"])
        self.assertTrue(result["fallback_used"])

    def test_negative_id_triggers_fallback(self):
        state = _base_state(unique_id=-1)
        result = _run(validate_output(state))
        self.assertTrue(result["fallback_used"])

    def test_wrong_machine_id_in_result_triggers_fallback(self):
        ts, mid, seq = 1_700_000_000_000, 5, 3
        # Compute uid with wrong machine_id
        wrong_uid = _deterministic_snowflake(ts, mid + 1, seq)
        state = _base_state(timestamp_ms=ts, machine_id=mid, sequence=seq,
                            unique_id=wrong_uid)
        result = _run(validate_output(state))
        self.assertTrue(result["fallback_used"])

    def test_fallback_result_has_correct_bit_layout(self):
        ts, mid, seq = 1_700_000_000_000, 7, 4
        state = _base_state(timestamp_ms=ts, machine_id=mid, sequence=seq,
                            unique_id=None)
        result = _run(validate_output(state))
        uid = result["unique_id"]
        self.assertEqual(uid >> _TIMESTAMP_SHIFT,           ts)
        self.assertEqual((uid >> _MACHINE_ID_SHIFT) & 0x3FF, mid)
        self.assertEqual(uid & 0xFFF,                        seq)

    def test_timestamp_off_by_one_still_passes(self):
        """±1 ms tolerance — the LLM may round the timestamp."""
        ts, mid, seq = 1_700_000_000_000, 3, 1
        uid_off_by_one = _deterministic_snowflake(ts + 1, mid, seq)
        state = _base_state(timestamp_ms=ts, machine_id=mid, sequence=seq,
                            unique_id=uid_off_by_one)
        result = _run(validate_output(state))
        # ±1 is tolerated — should pass without fallback
        self.assertFalse(result["fallback_used"])


# ============================================================
# Full graph (all nodes, LLM mocked)
# ============================================================

class TestFullGraph(unittest.TestCase):

    def _run_graph(self, ts=1_700_000_000_000, mid=0, seq=0, llm_id=None, malformed=False):
        """Run the full compiled graph with a mocked LLM."""
        graph = build_unique_id_agent()

        if malformed:
            mock_resp = MagicMock()
            mock_resp.text.return_value = "bad output"
            mock_resp.usage_metadata = {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
        else:
            expected = llm_id or _deterministic_snowflake(ts, mid, seq)
            mock_resp = _make_llm_response(expected)

        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = mock_resp
            state = _base_state(timestamp_ms=ts, machine_id=mid, sequence=seq)
            return _run(graph.ainvoke(state))

    def test_graph_returns_positive_id(self):
        result = self._run_graph()
        self.assertGreater(result["unique_id"], 0)

    def test_graph_returns_correct_id_when_llm_correct(self):
        ts, mid, seq = 1_700_000_000_000, 3, 7
        expected = _deterministic_snowflake(ts, mid, seq)
        result   = self._run_graph(ts=ts, mid=mid, seq=seq, llm_id=expected)
        self.assertEqual(result["unique_id"], expected)
        self.assertFalse(result["fallback_used"])

    def test_graph_uses_fallback_when_llm_wrong(self):
        ts, mid, seq = 1_700_000_000_000, 2, 5
        expected = _deterministic_snowflake(ts, mid, seq)
        result   = self._run_graph(ts=ts, mid=mid, seq=seq, llm_id=42)  # wrong
        self.assertEqual(result["unique_id"], expected)
        self.assertTrue(result["fallback_used"])

    def test_graph_uses_fallback_when_llm_malformed(self):
        result = self._run_graph(malformed=True)
        self.assertIsNotNone(result["unique_id"])
        self.assertTrue(result["fallback_used"])

    def test_graph_tracks_llm_metrics(self):
        result = self._run_graph()
        self.assertEqual(result["total_llm_calls"],     1)
        self.assertEqual(result["total_input_tokens"],  50)
        self.assertEqual(result["total_output_tokens"], 10)

    def test_graph_idempotent_multiple_runs(self):
        """Running the graph twice with the same inputs produces the same ID."""
        ts, mid, seq = 1_700_000_000_000, 0, 0
        expected = _deterministic_snowflake(ts, mid, seq)
        graph = build_unique_id_agent()
        mock_resp = _make_llm_response(expected)
        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = mock_resp
            state = _base_state(timestamp_ms=ts, machine_id=mid, sequence=seq)
            r1 = _run(graph.ainvoke(state))
            r2 = _run(graph.ainvoke(state))
        self.assertEqual(r1["unique_id"], r2["unique_id"])


# ============================================================
# Thrift handler (LLM mocked)
# ============================================================

class TestUniqueIdHandler(unittest.TestCase):

    def _make_handler(self, machine_id=0, llm_id=None, malformed=False):
        from handler import UniqueIdHandler
        gen    = AgentSnowflakeGenerator(machine_id)
        tracer = opentracing.tracer
        h      = UniqueIdHandler(gen, tracer)

        if malformed:
            mock_resp = MagicMock()
            mock_resp.text.return_value = "bad llm output"
            mock_resp.usage_metadata = {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
        else:
            ts, mid, seq = gen.next_inputs()   # peek at next inputs
            # Reset so handler sees same values
            gen._last_ms = ts - 1
            gen._sequence = seq - 1 if seq > 0 else 0
            uid = llm_id or _deterministic_snowflake(ts, machine_id, seq)
            mock_resp = _make_llm_response(uid)

        return h, mock_resp

    def test_compose_unique_id_returns_positive_int(self):
        h, mock_resp = self._make_handler()
        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = mock_resp
            uid = h.ComposeUniqueId(1, PostType.POST, {})
        self.assertIsInstance(uid, int)
        self.assertGreater(uid, 0)

    def test_compose_unique_id_all_post_types(self):
        for pt in [PostType.POST, PostType.REPOST, PostType.REPLY, PostType.DM]:
            h, mock_resp = self._make_handler()
            with patch("agent.llm") as mock_llm:
                mock_llm.invoke.return_value = mock_resp
                uid = h.ComposeUniqueId(1, pt, {})
            self.assertGreater(uid, 0)

    def test_compose_unique_id_with_carrier(self):
        h, mock_resp = self._make_handler()
        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = mock_resp
            uid = h.ComposeUniqueId(1, PostType.POST, {"uber-trace-id": "abc"})
        self.assertGreater(uid, 0)

    def test_fallback_when_llm_malformed(self):
        """Even with malformed LLM output, handler must return a valid ID."""
        h, mock_resp = self._make_handler(malformed=True)
        with patch("agent.llm") as mock_llm:
            mock_llm.invoke.return_value = mock_resp
            uid = h.ComposeUniqueId(1, PostType.POST, {})
        self.assertGreater(uid, 0)

    def test_clock_skew_raises_service_exception(self):
        from handler import UniqueIdHandler
        gen = AgentSnowflakeGenerator(0)
        gen._last_ms = int(time.time() * 1000) + 9_999_999
        h = UniqueIdHandler(gen, opentracing.tracer)
        with self.assertRaises(ServiceException) as ctx:
            h.ComposeUniqueId(1, PostType.POST, {})
        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_THRIFT_HANDLER_ERROR)

    def test_ids_are_unique_sequential(self):
        """10 sequential calls must return 10 distinct IDs."""
        from handler import UniqueIdHandler
        gen    = AgentSnowflakeGenerator(0)
        tracer = opentracing.tracer
        h      = UniqueIdHandler(gen, tracer)
        ids = []
        for i in range(10):
            ts, mid, seq = gen._last_ms + 1, 0, i % 4096
            uid = _deterministic_snowflake(ts, mid, seq)
            mock_resp = _make_llm_response(uid)
            with patch("agent.llm") as mock_llm:
                mock_llm.invoke.return_value = mock_resp
                ids.append(h.ComposeUniqueId(i, PostType.POST, {}))
        self.assertEqual(len(set(ids)), 10)


# ============================================================
# Thrift round-trip over loopback
# ============================================================

class TestThriftRoundTrip(unittest.TestCase):

    PORT = 19990

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import UniqueIdService as UIS
        from handler import UniqueIdHandler

        gen    = AgentSnowflakeGenerator(0)
        tracer = opentracing.tracer
        h      = UniqueIdHandler(gen, tracer)
        processor = UIS.Processor(h)

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
        from gen_py.social_network import UniqueIdService as UIS
        sock      = TSocket.TSocket("127.0.0.1", self.PORT)
        transport = TTransport.TFramedTransport(sock)
        protocol  = TBinaryProtocol.TBinaryProtocol(transport)
        client    = UIS.Client(protocol)
        transport.open()
        return client, transport

    def _mock_llm(self, ts, mid, seq):
        uid = _deterministic_snowflake(ts, mid, seq)
        resp = _make_llm_response(uid)
        return resp

    def test_round_trip_returns_positive_id(self):
        client, transport = self._client()
        try:
            with patch("agent.llm") as mock_llm:
                mock_llm.invoke.side_effect = lambda prompt: _make_llm_response(
                    _deterministic_snowflake(1_700_000_000_000, 0, 0)
                )
                uid = client.ComposeUniqueId(1, PostType.POST, {})
            self.assertGreater(uid, 0)
        finally:
            transport.close()

    def test_round_trip_10_ids_unique(self):
        client, transport = self._client()
        ids = []
        try:
            with patch("agent.llm") as mock_llm:
                counter = [0]
                def side_effect(prompt):
                    counter[0] += 1
                    return _make_llm_response(
                        _deterministic_snowflake(1_700_000_000_000 + counter[0], 0, counter[0] % 4096)
                    )
                mock_llm.invoke.side_effect = side_effect
                for i in range(10):
                    ids.append(client.ComposeUniqueId(i, PostType.POST, {}))
        finally:
            transport.close()
        self.assertEqual(len(set(ids)), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
