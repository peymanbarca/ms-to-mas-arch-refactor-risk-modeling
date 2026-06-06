"""
Tests for the Python UniqueIdService port.

Run with:
    cd unique-id-service
    python -m pytest test_unique_id.py -v
"""

import sys
import os
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from gen_py.social_network.ttypes import PostType, ServiceException, ErrorCode
from snowflake import SnowflakeGenerator, _TIMESTAMP_SHIFT, _MACHINE_ID_SHIFT, _SEQUENCE_BITS
from handler import UniqueIdHandler


# ---------------------------------------------------------------------------
# SnowflakeGenerator unit tests
# ---------------------------------------------------------------------------

class TestSnowflakeGenerator(unittest.TestCase):

    def _make_gen(self, machine_id=0):
        return SnowflakeGenerator(machine_id)

    # --- Basic correctness ---

    def test_returns_positive_integer(self):
        gen = self._make_gen()
        uid = gen.next_id()
        self.assertIsInstance(uid, int)
        self.assertGreater(uid, 0)

    def test_ids_are_unique_sequential(self):
        gen = self._make_gen()
        ids = [gen.next_id() for _ in range(10000)]
        self.assertEqual(len(set(ids)), 10000, "Duplicate IDs detected")

    def test_ids_are_monotonically_increasing(self):
        gen = self._make_gen()
        ids = [gen.next_id() for _ in range(1000)]
        for a, b in zip(ids, ids[1:]):
            self.assertLess(a, b, "IDs must be monotonically increasing")

    # --- Bit-field extraction ---

    def test_machine_id_embedded_correctly(self):
        machine_id = 42
        gen = SnowflakeGenerator(machine_id)
        uid = gen.next_id()
        extracted = (uid >> _MACHINE_ID_SHIFT) & ((1 << 10) - 1)
        self.assertEqual(extracted, machine_id)

    def test_sequence_starts_at_zero_on_new_millisecond(self):
        gen = self._make_gen(0)
        # Force a known last_ms so that the next call lands in a new ms
        gen._last_ms = -1
        uid = gen.next_id()
        sequence = uid & ((1 << _SEQUENCE_BITS) - 1)
        self.assertEqual(sequence, 0)

    def test_timestamp_is_embedded_and_recent(self):
        gen = self._make_gen()
        before_ms = int(time.time() * 1000)
        uid = gen.next_id()
        after_ms  = int(time.time() * 1000)
        extracted_ms = uid >> _TIMESTAMP_SHIFT
        self.assertGreaterEqual(extracted_ms, before_ms)
        self.assertLessEqual(extracted_ms, after_ms)

    def test_different_machine_ids_produce_different_ids_at_same_ms(self):
        gen0 = SnowflakeGenerator(0)
        gen1 = SnowflakeGenerator(1)
        # Force both to same fake timestamp
        fake_ms = int(time.time() * 1000)
        gen0._last_ms = fake_ms - 1
        gen1._last_ms = fake_ms - 1
        uid0 = gen0.next_id()
        uid1 = gen1.next_id()
        self.assertNotEqual(uid0, uid1)

    # --- Bounds / validation ---

    def test_invalid_machine_id_raises(self):
        with self.assertRaises(ValueError):
            SnowflakeGenerator(-1)
        with self.assertRaises(ValueError):
            SnowflakeGenerator(1024)  # max is 1023

    def test_max_machine_id_accepted(self):
        gen = SnowflakeGenerator(1023)
        self.assertIsNotNone(gen.next_id())

    # --- Thread safety ---

    def test_thread_safety_no_duplicates(self):
        gen = self._make_gen(7)
        results = []
        lock = threading.Lock()
        errors = []

        def worker():
            try:
                local = [gen.next_id() for _ in range(500)]
                with lock:
                    results.extend(local)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Errors during concurrent generation: {errors}")
        self.assertEqual(
            len(results), len(set(results)),
            f"Duplicates found in {len(results)} concurrent IDs",
        )

    # --- Sequence overflow (stress) ---

    def test_sequence_overflow_produces_unique_ids(self):
        """
        Generate more than 4096 IDs in rapid succession to force at least one
        sequence overflow and verify no duplicates occur.
        """
        gen = self._make_gen(0)
        ids = [gen.next_id() for _ in range(5000)]
        self.assertEqual(len(set(ids)), 5000)

    # --- Clock skew ---

    def test_clock_backwards_raises_runtime_error(self):
        gen = self._make_gen()
        # Inject a last_ms far in the future
        gen._last_ms = int(time.time() * 1000) + 60_000
        with self.assertRaises(RuntimeError):
            with gen._lock:
                gen._generate()


# ---------------------------------------------------------------------------
# UniqueIdHandler unit tests
# ---------------------------------------------------------------------------

class TestUniqueIdHandler(unittest.TestCase):

    def _make_handler(self, machine_id=0):
        gen     = SnowflakeGenerator(machine_id)
        tracer  = opentracing.tracer   # no-op
        return UniqueIdHandler(gen, tracer)

    def test_compose_unique_id_returns_int(self):
        h = self._make_handler()
        uid = h.ComposeUniqueId(req_id=1, post_type=PostType.POST, carrier={})
        self.assertIsInstance(uid, int)
        self.assertGreater(uid, 0)

    def test_compose_unique_id_all_post_types(self):
        h = self._make_handler()
        seen = set()
        for pt in [PostType.POST, PostType.REPOST, PostType.REPLY, PostType.DM]:
            uid = h.ComposeUniqueId(req_id=1, post_type=pt, carrier={})
            self.assertNotIn(uid, seen)
            seen.add(uid)

    def test_compose_unique_id_with_carrier_headers(self):
        """Handler must not crash when carrier contains trace headers."""
        h = self._make_handler()
        carrier = {
            "uber-trace-id": "abc123:def456:0:1",
            "x-b3-traceid":  "abc123",
        }
        uid = h.ComposeUniqueId(req_id=42, post_type=PostType.POST, carrier=carrier)
        self.assertGreater(uid, 0)

    def test_compose_unique_id_propagates_clock_error_as_service_exception(self):
        """Clock skew in the generator must surface as ServiceException."""
        gen    = SnowflakeGenerator(0)
        tracer = opentracing.tracer
        h      = UniqueIdHandler(gen, tracer)

        # Force clock skew
        gen._last_ms = int(time.time() * 1000) + 9_999_999

        with self.assertRaises(ServiceException) as ctx:
            h.ComposeUniqueId(req_id=1, post_type=PostType.POST, carrier={})

        self.assertEqual(ctx.exception.errorCode, ErrorCode.SE_THRIFT_HANDLER_ERROR)

    def test_handler_is_thread_safe(self):
        h = self._make_handler(machine_id=3)
        results = []
        lock = threading.Lock()

        def worker():
            local = [
                h.ComposeUniqueId(req_id=i, post_type=PostType.POST, carrier={})
                for i in range(200)
            ]
            with lock:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(len(results), len(set(results)), "Duplicate IDs from handler")


# ---------------------------------------------------------------------------
# Integration smoke-test: full Thrift round-trip over loopback
# ---------------------------------------------------------------------------

class TestThriftRoundTrip(unittest.TestCase):
    """
    Starts the actual TThreadedServer on a random port, connects a Thrift
    client, and calls ComposeUniqueId — mimicking what ComposePostService does.
    """

    @classmethod
    def setUpClass(cls):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from thrift.server    import TServer
        from gen_py.social_network import UniqueIdService as UIS

        cls.PORT = 19999

        gen     = SnowflakeGenerator(0)
        tracer  = opentracing.tracer
        handler = UniqueIdHandler(gen, tracer)
        processor = UIS.Processor(handler)

        # Force IPv4 to avoid Errno 97 in sandboxed environments
        transport = TSocket.TServerSocket(host="127.0.0.1", port=cls.PORT)
        tfactory  = TTransport.TFramedTransportFactory()
        pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

        cls.server = TServer.TThreadedServer(
            processor, transport, tfactory, pfactory, daemon=True
        )

        cls.server_thread = threading.Thread(
            target=cls.server.serve, daemon=True
        )
        cls.server_thread.start()
        time.sleep(0.5)   # give the server a moment to bind

    def _make_client(self):
        from thrift.transport import TSocket, TTransport
        from thrift.protocol  import TBinaryProtocol
        from gen_py.social_network import UniqueIdService as UIS

        sock      = TSocket.TSocket("127.0.0.1", self.PORT)
        transport = TTransport.TFramedTransport(sock)
        protocol  = TBinaryProtocol.TBinaryProtocol(transport)
        client    = UIS.Client(protocol)
        transport.open()
        return client, transport

    def test_round_trip_returns_positive_id(self):
        client, transport = self._make_client()
        try:
            uid = client.ComposeUniqueId(
                req_id=1, post_type=PostType.POST, carrier={}
            )
            self.assertGreater(uid, 0)
        finally:
            transport.close()

    def test_round_trip_100_ids_unique(self):
        client, transport = self._make_client()
        try:
            ids = [
                client.ComposeUniqueId(
                    req_id=i, post_type=PostType.POST, carrier={}
                )
                for i in range(100)
            ]
            self.assertEqual(len(set(ids)), 100)
        finally:
            transport.close()

    def test_round_trip_concurrent_clients(self):
        results = []
        lock = threading.Lock()

        def run():
            client, transport = self._make_client()
            try:
                local = [
                    client.ComposeUniqueId(
                        req_id=i, post_type=PostType.REPLY, carrier={}
                    )
                    for i in range(50)
                ]
                with lock:
                    results.extend(local)
            finally:
                transport.close()

        threads = [threading.Thread(target=run) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(len(results), 250)
        self.assertEqual(len(set(results)), 250, "Duplicate IDs across concurrent clients")


if __name__ == "__main__":
    unittest.main(verbosity=2)
