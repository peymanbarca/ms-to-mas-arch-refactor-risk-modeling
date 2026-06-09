"""
Tests for WriteHomeTimelineService Python port.

Run with:
    cd write-home-timeline-service
    PYTHONPATH=gen-py python -m pytest test_write_home_timeline.py -v
"""

import sys
import os
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

import opentracing
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode

from .message  import encode, decode, WriteHomeTimelineMessage
from .worker   import MessageWorker
from .consumer import WriteHomeTimelineConsumer


# ============================================================
# message.py — encode / decode
# ============================================================

class TestMessageEncoding(unittest.TestCase):

    def _msg(self, **kwargs):
        defaults = dict(
            req_id=1, post_id=42, user_id=7,
            timestamp=9000, user_mentions_id=[10, 20],
            carrier={"x-trace": "abc"},
        )
        defaults.update(kwargs)
        return WriteHomeTimelineMessage(**defaults)

    def test_encode_returns_bytes(self):
        self.assertIsInstance(encode(self._msg()), bytes)

    def test_decode_round_trip(self):
        msg  = self._msg()
        raw  = encode(msg)
        msg2 = decode(raw)
        self.assertEqual(msg2.req_id,           msg.req_id)
        self.assertEqual(msg2.post_id,          msg.post_id)
        self.assertEqual(msg2.user_id,          msg.user_id)
        self.assertEqual(msg2.timestamp,        msg.timestamp)
        self.assertEqual(msg2.user_mentions_id, msg.user_mentions_id)
        self.assertEqual(msg2.carrier,          msg.carrier)

    def test_decode_all_int_fields_are_int(self):
        msg = decode(encode(self._msg()))
        self.assertIsInstance(msg.req_id,    int)
        self.assertIsInstance(msg.post_id,   int)
        self.assertIsInstance(msg.user_id,   int)
        self.assertIsInstance(msg.timestamp, int)
        for uid in msg.user_mentions_id:
            self.assertIsInstance(uid, int)

    def test_decode_empty_mentions(self):
        msg = decode(encode(self._msg(user_mentions_id=[])))
        self.assertEqual(msg.user_mentions_id, [])

    def test_decode_empty_carrier(self):
        msg = decode(encode(self._msg(carrier={})))
        self.assertEqual(msg.carrier, {})

    def test_encode_is_valid_json(self):
        raw = encode(self._msg())
        parsed = json.loads(raw)
        self.assertIn("post_id", parsed)
        self.assertIn("user_id", parsed)

    def test_decode_missing_carrier_defaults_to_empty(self):
        """Backwards compatibility: old messages without 'carrier' field."""
        raw = json.dumps({
            "req_id": 1, "post_id": 2, "user_id": 3,
            "timestamp": 4, "user_mentions_id": [],
        }).encode()
        msg = decode(raw)
        self.assertEqual(msg.carrier, {})

    def test_large_i64_values(self):
        large = 2**40
        msg = decode(encode(self._msg(post_id=large, user_id=large + 1)))
        self.assertEqual(msg.post_id, large)
        self.assertEqual(msg.user_id, large + 1)

    def test_multiple_user_mentions(self):
        mentions = list(range(100))
        msg = decode(encode(self._msg(user_mentions_id=mentions)))
        self.assertEqual(msg.user_mentions_id, mentions)


# ============================================================
# MessageWorker — process()
# ============================================================

def _make_worker(call_result=None, call_exc=None):
    """Return a MessageWorker with a mocked HomeTimelineService pool."""
    home_client = MagicMock()
    if call_exc:
        home_client.WriteHomeTimeline.side_effect = call_exc
    else:
        home_client.WriteHomeTimeline.return_value = call_result

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=home_client)
    cm.__exit__  = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value = cm

    worker = MessageWorker(home_timeline_pool=pool, tracer=opentracing.tracer)
    return worker, home_client


def _encode_msg(**kwargs):
    defaults = dict(
        req_id=1, post_id=42, user_id=7,
        timestamp=9000, user_mentions_id=[10, 20], carrier={},
    )
    defaults.update(kwargs)
    return encode(WriteHomeTimelineMessage(**defaults))


class TestMessageWorker(unittest.TestCase):

    def test_calls_write_home_timeline(self):
        worker, client = _make_worker()
        worker.process(_encode_msg())
        client.WriteHomeTimeline.assert_called_once()

    def test_passes_correct_args(self):
        worker, client = _make_worker()
        worker.process(_encode_msg(
            req_id=5, post_id=99, user_id=3,
            timestamp=1234, user_mentions_id=[7, 8],
        ))
        args = client.WriteHomeTimeline.call_args[0]
        self.assertEqual(args[0], 5)      # req_id
        self.assertEqual(args[1], 99)     # post_id
        self.assertEqual(args[2], 3)      # user_id
        self.assertEqual(args[3], 1234)   # timestamp
        self.assertEqual(args[4], [7, 8]) # user_mentions_id

    def test_service_exception_raises(self):
        exc = ServiceException(
            errorCode=ErrorCode.SE_REDIS_ERROR, message="redis down"
        )
        worker, _ = _make_worker(call_exc=exc)
        with self.assertRaises(ServiceException):
            worker.process(_encode_msg())

    def test_transport_exception_raises(self):
        from thrift.transport.TTransport import TTransportException
        worker, _ = _make_worker(
            call_exc=TTransportException(message="conn reset")
        )
        with self.assertRaises(Exception):
            worker.process(_encode_msg())

    def test_malformed_body_raises(self):
        worker, client = _make_worker()
        with self.assertRaises(Exception):
            worker.process(b"not valid json {{{")

    def test_empty_mentions_works(self):
        worker, client = _make_worker()
        worker.process(_encode_msg(user_mentions_id=[]))
        args = client.WriteHomeTimeline.call_args[0]
        self.assertEqual(args[4], [])

    def test_carrier_passed_to_downstream(self):
        worker, client = _make_worker()
        worker.process(_encode_msg(carrier={"x-trace": "abc"}))
        # carrier arg is the last positional arg
        args = client.WriteHomeTimeline.call_args[0]
        self.assertIsInstance(args[5], dict)

    def test_concurrent_processing(self):
        """Multiple threads can call process() concurrently (worker is shared)."""
        results = []
        errors  = []
        lock    = threading.Lock()
        worker, _ = _make_worker()

        def run(i):
            try:
                worker.process(_encode_msg(req_id=i, post_id=i))
                with lock:
                    results.append(i)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 20)


# ============================================================
# WriteHomeTimelineConsumer — ACK / NACK logic
# ============================================================

class TestConsumerCallback(unittest.TestCase):
    """Test the on_message callback behaviour without real RabbitMQ."""

    def _make_callback(self, worker):
        """Extract the callback closure from a consumer instance."""
        rabbitmq_cfg = {"host": "localhost", "port": 5672}
        consumer = WriteHomeTimelineConsumer(
            rabbitmq_cfg=rabbitmq_cfg,
            queue_name="test-queue",
            worker=worker,
            num_workers=1,
        )
        return consumer._make_callback(worker_id=0)

    def _mock_channel(self):
        ch = MagicMock()
        return ch

    def _mock_method(self, tag=1):
        m = MagicMock()
        m.delivery_tag = tag
        return m

    def test_ack_on_success(self):
        worker, _ = _make_worker()
        cb      = self._make_callback(worker)
        channel = self._mock_channel()
        cb(channel, self._mock_method(42), MagicMock(), _encode_msg())
        channel.basic_ack.assert_called_once_with(delivery_tag=42)
        channel.basic_nack.assert_not_called()

    def test_nack_with_requeue_on_worker_error(self):
        exc = ServiceException(
            errorCode=ErrorCode.SE_REDIS_ERROR, message="redis down"
        )
        worker, _ = _make_worker(call_exc=exc)
        cb      = self._make_callback(worker)
        channel = self._mock_channel()
        cb(channel, self._mock_method(99), MagicMock(), _encode_msg())
        channel.basic_nack.assert_called_once_with(delivery_tag=99, requeue=True)
        channel.basic_ack.assert_not_called()

    def test_nack_on_malformed_message(self):
        worker, _ = _make_worker()
        cb      = self._make_callback(worker)
        channel = self._mock_channel()
        cb(channel, self._mock_method(7), MagicMock(), b"bad json {{{")
        channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=True)

    def test_ack_called_with_correct_delivery_tag(self):
        for tag in [1, 100, 99999]:
            worker, _ = _make_worker()
            cb      = self._make_callback(worker)
            channel = self._mock_channel()
            cb(channel, self._mock_method(tag), MagicMock(), _encode_msg())
            channel.basic_ack.assert_called_with(delivery_tag=tag)


# ============================================================
# Consumer start/stop lifecycle
# ============================================================

class TestConsumerLifecycle(unittest.TestCase):

    def test_stop_event_prevents_reconnect(self):
        """Calling stop() before start() leaves no threads alive."""
        worker, _ = _make_worker()
        consumer = WriteHomeTimelineConsumer(
            rabbitmq_cfg={"host": "localhost"},
            queue_name="q",
            worker=worker,
            num_workers=2,
        )
        # Stop before starting — threads list is empty
        consumer.stop()
        self.assertEqual(len(consumer._threads), 0)

    def test_start_creates_correct_number_of_threads(self):
        """Verify num_workers threads are spawned (without real RabbitMQ)."""
        worker, _ = _make_worker()
        consumer = WriteHomeTimelineConsumer(
            rabbitmq_cfg={"host": "localhost"},
            queue_name="q",
            worker=worker,
            num_workers=3,
        )
        # Patch _consume_blocking to immediately return (no real RabbitMQ needed)
        consumer._stop_event.set()   # signal stop immediately
        consumer.start()
        # Threads should be created
        self.assertEqual(len(consumer._threads), 3)
        # All daemon threads
        for t in consumer._threads:
            self.assertTrue(t.daemon)


# ============================================================
# Publisher
# ============================================================

class TestPublisher(unittest.TestCase):

    def test_publish_sends_correct_payload(self):
        from publisher import HomeTimelinePublisher
        with patch("publisher.pika.BlockingConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_conn.is_closed = False
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_conn_cls.return_value = mock_conn

            pub = HomeTimelinePublisher(
                {"host": "localhost", "port": 5672}, "test-queue"
            )
            pub.connect()
            pub.publish(
                req_id=1, post_id=42, user_id=7,
                timestamp=9000, user_mentions_id=[10, 20],
                carrier={"x-trace": "abc"},
            )

            mock_channel.basic_publish.assert_called_once()
            call_kwargs = mock_channel.basic_publish.call_args[1]
            self.assertEqual(call_kwargs["routing_key"], "test-queue")

            body = call_kwargs["body"]
            decoded = json.loads(body)
            self.assertEqual(decoded["post_id"],          42)
            self.assertEqual(decoded["user_id"],          7)
            self.assertEqual(decoded["user_mentions_id"], [10, 20])

    def test_context_manager_closes(self):
        from publisher import HomeTimelinePublisher
        with patch("publisher.pika.BlockingConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_conn.is_closed = False
            mock_conn.channel.return_value = MagicMock()
            mock_conn_cls.return_value = mock_conn

            with HomeTimelinePublisher({"host": "localhost"}) as pub:
                pub.publish(1, 42, 7, 9000, [])

            mock_conn.close.assert_called()

    def test_publish_message_is_persistent(self):
        """Delivery mode 2 = persistent message."""
        import pika as pika_lib
        from publisher import HomeTimelinePublisher
        with patch("publisher.pika.BlockingConnection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_conn.is_closed = False
            mock_channel = MagicMock()
            mock_conn.channel.return_value = mock_channel
            mock_conn_cls.return_value = mock_conn

            with HomeTimelinePublisher({"host": "localhost"}) as pub:
                pub.publish(1, 1, 1, 1000, [])

            props = mock_channel.basic_publish.call_args[1]["properties"]
            self.assertEqual(props.delivery_mode, 2)


# ============================================================
# End-to-end: publish → consume → WriteHomeTimeline called
# ============================================================

class TestEndToEnd(unittest.TestCase):
    """
    Simulates the full publish → consume → WriteHomeTimeline flow
    without real RabbitMQ or HomeTimelineService.
    """

    def test_encode_decode_worker_pipeline(self):
        """Encoded message → decoded → worker processes correctly."""
        worker, home_client = _make_worker()

        original = WriteHomeTimelineMessage(
            req_id=100, post_id=999, user_id=5,
            timestamp=8888, user_mentions_id=[11, 22, 33],
            carrier={"uber-trace-id": "x:y:0:1"},
        )
        body = encode(original)
        worker.process(body)

        home_client.WriteHomeTimeline.assert_called_once()
        args = home_client.WriteHomeTimeline.call_args[0]
        self.assertEqual(args[0], 100)          # req_id
        self.assertEqual(args[1], 999)          # post_id
        self.assertEqual(args[2], 5)            # user_id
        self.assertEqual(args[3], 8888)         # timestamp
        self.assertEqual(args[4], [11, 22, 33]) # user_mentions_id

    def test_ack_flow_end_to_end(self):
        """Full ACK flow: message in → HomeTimeline called → ACK out."""
        worker, home_client = _make_worker()
        consumer = WriteHomeTimelineConsumer(
            rabbitmq_cfg={"host": "localhost"},
            queue_name="q",
            worker=worker,
            num_workers=1,
        )
        cb      = consumer._make_callback(0)
        channel = MagicMock()
        method  = MagicMock()
        method.delivery_tag = 77

        cb(channel, method, MagicMock(), encode(WriteHomeTimelineMessage(
            req_id=1, post_id=2, user_id=3, timestamp=4,
            user_mentions_id=[], carrier={},
        )))

        home_client.WriteHomeTimeline.assert_called_once()
        channel.basic_ack.assert_called_once_with(delivery_tag=77)


if __name__ == "__main__":
    unittest.main(verbosity=2)
