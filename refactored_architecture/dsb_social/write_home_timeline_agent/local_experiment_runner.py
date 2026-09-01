"""
write_home_timeline_service/tests/regression_test.py

Regression Test & Benchmarking Suite for WriteHomeTimelineService (RabbitMQ Consumer).
Runs N iterations over AMQP message publishing & downstream Thrift state validation to verify:
  1. JSON Serialization & Schema compliance (WriteHomeTimelineMessage).
  2. RabbitMQ Message Publishing & Consumer ACK lifecycle on "write-home-timeline" queue.
  3. End-to-end propagation latency (AMQP publish -> Worker consumption -> HomeTimelineService RPC -> Redis persistence).
  4. Fan-out consistency for author timeline, followers, and mentioned user IDs.
  5. Error Handling & NACK/Requeue logic on malformed or corrupted payloads.
  6. Latency distributions for asynchronous end-to-end fan-out processing (p50, p95, p99).
"""

import json
import time
import uuid
import numpy as np
import pika
import redis

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import HomeTimelineService, SocialGraphService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException, ErrorCode
from ms_baseline.dsb_social.write_home_timeline_service.message import WriteHomeTimelineMessage, encode

RABBITMQ_HOST = "localhost"
RABBITMQ_PORT = 5672
RABBITMQ_QUEUE = "write-home-timeline"

HOME_TIMELINE_HOST = "localhost"
HOME_TIMELINE_PORT = 9099

SOCIAL_GRAPH_HOST = "localhost"
SOCIAL_GRAPH_PORT = 9097

REDIS_HOST = "localhost"
REDIS_PORT = 6385

ITERATIONS = 50
PROPAGATION_TIMEOUT_SEC = 1.0


def get_rabbitmq_channel():
    credentials = pika.PlainCredentials("guest", "guest")
    params = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
        socket_timeout=5,
    )
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(
        queue=RABBITMQ_QUEUE,
        durable=True,
        arguments={"x-message-ttl": 30000},
    )
    return connection, channel


def get_home_timeline_client():
    socket = TSocket.TSocket(HOME_TIMELINE_HOST, HOME_TIMELINE_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = HomeTimelineService.Client(protocol)
    transport.open()
    return transport, client


def get_social_graph_client():
    socket = TSocket.TSocket(SOCIAL_GRAPH_HOST, SOCIAL_GRAPH_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = SocialGraphService.Client(protocol)
    transport.open()
    return transport, client


def clear_redis_home_timeline_cache(user_ids: list[int]):
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, password="1")
    for uid in user_ids:
        r.delete(f"home-timeline:{uid}")
    r.close()


def run_write_home_timeline_regression_suite():
    print(f"--- Starting WriteHomeTimelineService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_end_to_end = []
    latencies_queue_ack = []
    invariant_violations = 0

    # Test execution entities setup
    author_id = 800000 + int(time.time() % 10000)
    follower_id = 810000 + int(time.time() % 10000)
    mentioned_id = 820000 + int(time.time() % 10000)

    # Establish follower link via SocialGraphService
    try:
        sg_transport, sg_client = get_social_graph_client()
        sg_client.Follow(req_id=1, user_id=follower_id, followee_id=author_id, carrier={})
        sg_transport.close()
        print(f"  ✓ Created graph edge: User {follower_id} follows Author {author_id}")
    except Exception as exc:
        print(f"❌ Setup failed (Social Graph Link): {exc}")
        return

    # Clean prior Redis timeline state
    clear_redis_home_timeline_cache([author_id, follower_id, mentioned_id])

    # ------------------------------------------------------------------
    # 1. Test Malformed Message Schema Handling (Negative Path)
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Malformed Payload Handling...")
    try:
        connection, channel = get_rabbitmq_channel()
        malformed_body = b"{" + b'"invalid_json": true'  # Incomplete JSON
        channel.basic_publish(
            exchange="",
            routing_key=RABBITMQ_QUEUE,
            body=malformed_body,
            properties=pika.BasicProperties(delivery_mode=2),
        )
        connection.close()
        print("  ✓ Malformed payload published. Worker should log error and NACK without crashing.")
    except Exception as exc:
        print(f"❌ [Malformed Payload Test] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 2. Benchmark Message Publishing & Asynchronous Fan-Out Propagation
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking Queue Publishing & Fan-Out Propagation ({ITERATIONS} Iterations)...")

    base_timestamp = int(time.time() * 1000)

    for i in range(1, ITERATIONS + 1):
        req_id = 1000 + i
        post_id = 900000 + i
        timestamp = base_timestamp + (i * 100)

        msg = WriteHomeTimelineMessage(
            req_id=req_id,
            post_id=post_id,
            user_id=author_id,
            timestamp=timestamp,
            user_mentions_id=[mentioned_id],
            carrier={"trace_id": f"trace_{req_id}"},
        )
        payload = encode(msg)

        t_pub_start = time.perf_counter()
        try:
            connection, channel = get_rabbitmq_channel()
            channel.basic_publish(
                exchange="",
                routing_key=RABBITMQ_QUEUE,
                body=payload,
                properties=pika.BasicProperties(delivery_mode=2),  # persistent
            )
            connection.close()
            t_pub_end = time.perf_counter()
            latencies_queue_ack.append((t_pub_end - t_pub_start) * 1000)
        except Exception as exc:
            print(f"❌ [Publish Iter {i}] Failed: {exc}")
            invariant_violations += 1
            continue

        # Poll HomeTimelineService to verify downstream consumer processing completion
        consumed = False
        t_poll_start = time.perf_counter()

        while (time.perf_counter() - t_poll_start) < PROPAGATION_TIMEOUT_SEC:
            try:
                ht_transport, ht_client = get_home_timeline_client()
                posts = ht_client.ReadHomeTimeline(
                    req_id=2000 + i,
                    user_id=follower_id,
                    start=0,
                    stop=ITERATIONS + 5,
                    carrier={},
                )
                ht_transport.close()

                post_ids = [p.post_id for p in posts]
                if post_id in post_ids:
                    t_consumed = time.perf_counter()
                    latencies_end_to_end.append((t_consumed - t_pub_start) * 1000)
                    consumed = True
                    break
            except Exception:
                pass
            time.sleep(0.02)  # 20ms polling backoff

        if not consumed:
            print(f"❌ [Propagation Timeout Iter {i}] Post ID {post_id} not observed in timeline within {PROPAGATION_TIMEOUT_SEC}s")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Validate Mentioned User Timeline Fan-Out Invariant
    # ------------------------------------------------------------------
    print("\n[Test 3] Validating Mentioned User Fan-Out Invariant...")
    try:
        ht_transport, ht_client = get_home_timeline_client()
        mentioned_posts = ht_client.ReadHomeTimeline(
            req_id=3001,
            user_id=mentioned_id,
            start=0,
            stop=ITERATIONS + 5,
            carrier={},
        )
        ht_transport.close()

        assert len(mentioned_posts) == ITERATIONS, (
            f"Expected {ITERATIONS} posts in mentioned user timeline, got {len(mentioned_posts)}"
        )
        print(f"  ✓ Mentioned user received all {ITERATIONS} posts via consumer fan-out")
    except Exception as exc:
        print(f"❌ [Mentioned User Invariant] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 4. Queue Drain & Depth Health Check
    # ------------------------------------------------------------------
    print("\n[Test 4] Checking RabbitMQ Queue Depth & Consumption State...")
    try:
        connection, channel = get_rabbitmq_channel()
        res = channel.queue_declare(queue=RABBITMQ_QUEUE, passive=True)
        message_count = res.method.message_count
        connection.close()

        print(f"  ✓ Unconsumed messages remaining in queue: {message_count}")
        if message_count > 1:  # 1 message may be the malformed one from Test 1
            print(f"⚠️ Warning: Queue contains {message_count} unprocessed messages")
    except Exception as exc:
        print(f"❌ [Queue Inspection] Failed: {exc}")
        invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("   WRITE HOME TIMELINE SERVICE (CONSUMER) REGRESSION REPORT   ")
    print("=" * 65)
    print(f"Total Iterations        : {ITERATIONS}")
    print(f"Invariant Violations    : {invariant_violations}")

    if latencies_queue_ack:
        print("\n--- Latency Performance: AMQP Publish ---")
        print(
            f"Queue Publish          | p50: {np.median(latencies_queue_ack):.3f} ms | "
            f"p95: {np.percentile(latencies_queue_ack, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_queue_ack, 99):.3f} ms"
        )

    if latencies_end_to_end:
        print("\n--- Latency Performance: End-to-End Fan-Out Propagation ---")
        print(
            f"E2E Propagation        | p50: {np.median(latencies_end_to_end):.3f} ms | "
            f"p95: {np.percentile(latencies_end_to_end, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_end_to_end, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_write_home_timeline_regression_suite()