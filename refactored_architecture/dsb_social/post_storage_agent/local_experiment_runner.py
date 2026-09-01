"""
regression_test.py

Regression Test & Benchmarking Suite for PostStorageService (Thrift RPC).
Runs N iterations over the Thrift interface to verify:
  1. StorePost (writing to MongoDB and priming Redis cache).
  2. Idempotent upsert logic on duplicate StorePost invocations.
  3. Single post read via ReadPost (Cache HIT vs Cache MISS fallthrough to MongoDB).
  4. Parallel bulk post reads via ReadPosts (preserving order & multi-threading performance).
  5. Error handling for non-existent post IDs (ServiceException code SE_THRIFT_HANDLER_ERROR).
  6. Latency distributions for single/bulk reads across Cache HIT vs Cache MISS (p50, p95, p99).
"""

import time
import uuid
import numpy as np
from pymongo import MongoClient
import redis

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import PostStorageService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Post,
    Creator,
    UserMention,
    Media,
    Url,
    PostType,
    ServiceException,
    ErrorCode,
)

THRIFT_HOST = "localhost"
THRIFT_PORT = 9096
MONGO_HOST = "localhost"
MONGO_PORT = 27017
REDIS_HOST = "localhost"
REDIS_PORT = 6385

ITERATIONS = 50
BULK_BATCH_SIZE = 10


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = PostStorageService.Client(protocol)
    transport.open()
    return transport, client


def clear_redis_keys(post_ids: list[int]):
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, password="1")
    for pid in post_ids:
        r.delete(str(pid))
    r.close()


def create_dummy_post(post_id: int, req_id: int) -> Post:
    return Post(
        post_id=post_id,
        creator=Creator(user_id=1001, username="author_test"),
        req_id=req_id,
        text=f"Benchmark post body content for post_id={post_id}",
        user_mentions=[UserMention(user_id=2002, username="mentioned_user")],
        media=[Media(media_id=3003, media_type="png")],
        urls=[Url(shortened_url="http://short.ly/xyz", expanded_url="http://example.org/long")],
        timestamp=int(time.time()),
        post_type=PostType.POST,
    )


def run_post_storage_regression_suite():
    print(f"--- Starting PostStorageService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_store = []
    latencies_read_single_hit = []
    latencies_read_single_miss = []
    latencies_read_bulk_hit = []
    latencies_read_bulk_miss = []
    invariant_violations = 0

    base_post_id = int(time.time() * 1000)
    test_posts = [create_dummy_post(base_post_id + i, i) for i in range(ITERATIONS)]
    post_ids = [p.post_id for p in test_posts]

    # Ensure clean state in Redis before testing
    clear_redis_keys(post_ids)

    # ------------------------------------------------------------------
    # 1. Test Unregistered Post ID (Read Exception Handling)
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Non-Existent Post Exception Handler...")
    non_existent_pid = 99999999999
    try:
        transport, client = get_thrift_client()
        client.ReadPost(req_id=9999, post_id=non_existent_pid, carrier={})
        transport.close()
        print("❌ VIOLATION: Expected ServiceException on missing post_id")
        invariant_violations += 1
    except ServiceException as exc:
        if exc.errorCode == ErrorCode.SE_THRIFT_HANDLER_ERROR:
            print("  ✓ Correctly caught SE_THRIFT_HANDLER_ERROR for missing post_id")
        else:
            print(f"❌ VIOLATION: Unexpected errorCode: {exc.errorCode}")
            invariant_violations += 1
        try:
            transport.close()
        except Exception:
            pass
    except Exception as exc:
        print(f"❌ VIOLATION: Unexpected exception type: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 2. Benchmark StorePost & Verify Upsert Idempotency
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking StorePost ({ITERATIONS} Iterations) & Idempotency...")
    for i, post in enumerate(test_posts, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            client.StorePost(req_id=i, post=post, carrier={})
            t1 = time.perf_counter()
            latencies_store.append((t1 - t0) * 1000)

            # Test idempotency on first item (re-store same post_id)
            if i == 1:
                client.StorePost(req_id=i, post=post, carrier={})

            transport.close()
        except Exception as exc:
            print(f"❌ [StorePost Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Benchmark ReadPost (Cache Hit Scenario)
    # ------------------------------------------------------------------
    print(f"\n[Test 3] Benchmarking Single ReadPost (Cache HIT - {ITERATIONS} Iterations)...")
    for i, expected_post in enumerate(test_posts, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            fetched_post = client.ReadPost(req_id=100 + i, post_id=expected_post.post_id, carrier={})
            t1 = time.perf_counter()
            latencies_read_single_hit.append((t1 - t0) * 1000)
            transport.close()

            assert fetched_post.post_id == expected_post.post_id, f"[Hit Iter {i}] post_id mismatch"
            assert fetched_post.text == expected_post.text, f"[Hit Iter {i}] text mismatch"
            assert fetched_post.creator.username == expected_post.creator.username, (
                f"[Hit Iter {i}] creator mismatch"
            )
        except Exception as exc:
            print(f"❌ [ReadPost Hit Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 4. Benchmark ReadPost (Cache Miss / Mongo Read Scenario)
    # ------------------------------------------------------------------
    print(f"\n[Test 4] Benchmarking Single ReadPost (Cache MISS - {ITERATIONS} Iterations)...")
    clear_redis_keys(post_ids)  # Purge cache to force MongoDB access

    for i, expected_post in enumerate(test_posts, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            fetched_post = client.ReadPost(req_id=200 + i, post_id=expected_post.post_id, carrier={})
            t1 = time.perf_counter()
            latencies_read_single_miss.append((t1 - t0) * 1000)
            transport.close()

            assert fetched_post.post_id == expected_post.post_id, f"[Miss Iter {i}] post_id mismatch"
        except Exception as exc:
            print(f"❌ [ReadPost Miss Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 5. Benchmark ReadPosts (Bulk Read - Cache HIT & Order Preservation)
    # ------------------------------------------------------------------
    print(f"\n[Test 5] Benchmarking Bulk ReadPosts (Cache HIT - Batch Size {BULK_BATCH_SIZE})...")
    batch_count = len(post_ids) // BULK_BATCH_SIZE

    for b in range(batch_count):
        batch_pids = post_ids[b * BULK_BATCH_SIZE : (b + 1) * BULK_BATCH_SIZE]
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            returned_posts = client.ReadPosts(req_id=300 + b, post_ids=batch_pids, carrier={})
            t1 = time.perf_counter()
            latencies_read_bulk_hit.append((t1 - t0) * 1000)
            transport.close()

            assert len(returned_posts) == len(batch_pids), (
                f"[Bulk Hit Batch {b}] Length mismatch. Expected {len(batch_pids)}, got {len(returned_posts)}"
            )
            returned_ids = [p.post_id for p in returned_posts]
            assert returned_ids == batch_pids, (
                f"[Bulk Hit Batch {b}] Order preservation failure. Expected {batch_pids}, got {returned_ids}"
            )
        except Exception as exc:
            print(f"❌ [ReadPosts Bulk Hit Batch {b}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 6. Benchmark ReadPosts (Bulk Read - Cache MISS)
    # ------------------------------------------------------------------
    print(f"\n[Test 6] Benchmarking Bulk ReadPosts (Cache MISS - Batch Size {BULK_BATCH_SIZE})...")
    clear_redis_keys(post_ids)  # Purge Redis to force thread pool parallel Mongo queries

    for b in range(batch_count):
        batch_pids = post_ids[b * BULK_BATCH_SIZE : (b + 1) * BULK_BATCH_SIZE]
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            returned_posts = client.ReadPosts(req_id=400 + b, post_ids=batch_pids, carrier={})
            t1 = time.perf_counter()
            latencies_read_bulk_miss.append((t1 - t0) * 1000)
            transport.close()

            assert len(returned_posts) == len(batch_pids), f"[Bulk Miss Batch {b}] Length mismatch"
            returned_ids = [p.post_id for p in returned_posts]
            assert returned_ids == batch_pids, f"[Bulk Miss Batch {b}] Order preservation failure"
        except Exception as exc:
            print(f"❌ [ReadPosts Bulk Miss Batch {b}] Failed: {exc}")
            invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("    POST STORAGE SERVICE REGRESSION & LATENCY REPORT    ")
    print("=" * 65)
    print(f"Total Test Iterations  : {ITERATIONS}")
    print(f"Invariant Violations   : {invariant_violations}")

    if latencies_store:
        print("\n--- Latency Performance: StorePost ---")
        print(
            f"StorePost              | p50: {np.median(latencies_store):.3f} ms | "
            f"p95: {np.percentile(latencies_store, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_store, 99):.3f} ms"
        )

    if latencies_read_single_hit:
        print("\n--- Latency Performance: ReadPost (Single) ---")
        print(
            f"ReadPost (Hit)         | p50: {np.median(latencies_read_single_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_read_single_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_read_single_hit, 99):.3f} ms"
        )
        print(
            f"ReadPost (Miss)        | p50: {np.median(latencies_read_single_miss):.3f} ms | "
            f"p95: {np.percentile(latencies_read_single_miss, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_read_single_miss, 99):.3f} ms"
        )

    if latencies_read_bulk_hit:
        print(f"\n--- Latency Performance: ReadPosts (Bulk - Batch Size {BULK_BATCH_SIZE}) ---")
        print(
            f"ReadPosts (Bulk Hit)   | p50: {np.median(latencies_read_bulk_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_read_bulk_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_read_bulk_hit, 99):.3f} ms"
        )
        print(
            f"ReadPosts (Bulk Miss)  | p50: {np.median(latencies_read_bulk_miss):.3f} ms | "
            f"p95: {np.percentile(latencies_read_bulk_miss, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_read_bulk_miss, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_post_storage_regression_suite()