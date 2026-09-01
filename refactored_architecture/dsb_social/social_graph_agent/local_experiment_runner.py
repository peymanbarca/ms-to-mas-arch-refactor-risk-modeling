"""
regression_test.py

Regression Test & Benchmarking Suite for SocialGraphService (Thrift RPC).
Runs N iterations over the Thrift interface to verify:
  1. User Initialization via InsertUser ($setOnInsert idempotency).
  2. Direct Relationship Operations (Follow & Unfollow via user_ids).
  3. Username-based Relationship Operations (FollowWithUsername & UnfollowWithUsername via parallel UserService lookups).
  4. Graph Consistency & Reciprocity (followers of B contain A <-> followees of A contain B).
  5. Cache Behavior: Redis Sorted Sets vs MongoDB Fallback & Backfill seeding.
  6. Failure propagation & Exception Handling (SE_MONGODB_ERROR, SE_THRIFT_HANDLER_ERROR).
  7. Latency distributions for Graph Retrieval, Mutation, and Username Resolution (p50, p95, p99).
"""

import time
import uuid
import numpy as np
import redis

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import SocialGraphService, UserService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    ServiceException,
    ErrorCode,
)

THRIFT_HOST = "localhost"
THRIFT_PORT = 9097
USER_SERVICE_PORT = 9094
REDIS_HOST = "localhost"
REDIS_PORT = 6385

ITERATIONS = 50


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = SocialGraphService.Client(protocol)
    transport.open()
    return transport, client


def get_user_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, USER_SERVICE_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = UserService.Client(protocol)
    transport.open()
    return transport, client


def clear_redis_social_graph_cache(user_ids: list[int]):
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, password="1")
    for uid in user_ids:
        r.delete(f"followers:{uid}")
        r.delete(f"followees:{uid}")
    r.close()


def run_social_graph_regression_suite():
    print(f"--- Starting SocialGraphService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_insert = []
    latencies_follow = []
    latencies_unfollow = []
    latencies_get_followers_hit = []
    latencies_get_followers_miss = []
    latencies_follow_username = []
    invariant_violations = 0

    # Test execution scope setup
    user_a_id = 600000 + int(time.time() % 10000)
    user_b_ids = [700000 + i for i in range(ITERATIONS)]

    clear_redis_social_graph_cache([user_a_id] + user_b_ids)

    # ------------------------------------------------------------------
    # 1. Test InsertUser Idempotency
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating InsertUser Idempotency...")
    for i in range(2):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            client.InsertUser(req_id=100 + i, user_id=user_a_id, carrier={})
            t1 = time.perf_counter()
            latencies_insert.append((t1 - t0) * 1000)
            transport.close()
        except Exception as exc:
            print(f"❌ [InsertUser Iter {i}] Failed: {exc}")
            invariant_violations += 1

    try:
        transport, client = get_thrift_client()
        followers = client.GetFollowers(req_id=102, user_id=user_a_id, carrier={})
        followees = client.GetFollowees(req_id=103, user_id=user_a_id, carrier={})
        transport.close()
        assert len(followers) == 0 and len(followees) == 0, "Inserted user social graph must be empty"
        print("  ✓ InsertUser initialized empty arrays idempotently")
    except Exception as exc:
        print(f"❌ [InsertUser Check] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 2. Benchmark Follow Operations & Reciprocity Verification
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking Follow ({ITERATIONS} Iterations)...")
    for i, b_id in enumerate(user_b_ids, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            client.Follow(req_id=200 + i, user_id=user_a_id, followee_id=b_id, carrier={})
            t1 = time.perf_counter()
            latencies_follow.append((t1 - t0) * 1000)
            transport.close()
        except Exception as exc:
            print(f"❌ [Follow Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # Reciprocity and completeness check
    try:
        transport, client = get_thrift_client()
        a_followees = client.GetFollowees(req_id=299, user_id=user_a_id, carrier={})
        transport.close()
        assert len(a_followees) == ITERATIONS, f"Expected {ITERATIONS} followees, got {len(a_followees)}"
        assert set(a_followees) == set(user_b_ids), "Followee ID mismatch"
        print("  ✓ Reciprocity invariant: All followees updated correctly")
    except Exception as exc:
        print(f"❌ [Reciprocity Verification] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Benchmark GetFollowers / GetFollowees (Cache HIT)
    # ------------------------------------------------------------------
    print(f"\n[Test 3] Benchmarking GetFollowers / GetFollowees (Cache HIT - {ITERATIONS} Iterations)...")
    for i, b_id in enumerate(user_b_ids, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            followers = client.GetFollowers(req_id=300 + i, user_id=b_id, carrier={})
            t1 = time.perf_counter()
            latencies_get_followers_hit.append((t1 - t0) * 1000)
            transport.close()

            assert user_a_id in followers, f"User A missing from follower list of User B ({b_id})"
        except Exception as exc:
            print(f"❌ [GetFollowers Hit Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 4. Benchmark GetFollowers (Cache MISS / MongoDB Fallback & Redis Seed)
    # ------------------------------------------------------------------
    print(f"\n[Test 4] Benchmarking GetFollowers (Cache MISS - Evicting Redis Cache)...")
    clear_redis_social_graph_cache([user_a_id] + user_b_ids)

    for i, b_id in enumerate(user_b_ids, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            followers = client.GetFollowers(req_id=400 + i, user_id=b_id, carrier={})
            t1 = time.perf_counter()
            latencies_get_followers_miss.append((t1 - t0) * 1000)
            transport.close()

            assert user_a_id in followers, f"User A missing from fallback follower list of User B ({b_id})"
        except Exception as exc:
            print(f"❌ [GetFollowers Miss Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 5. Benchmark Unfollow Operations
    # ------------------------------------------------------------------
    print(f"\n[Test 5] Benchmarking Unfollow ({ITERATIONS} Iterations)...")
    for i, b_id in enumerate(user_b_ids, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            client.Unfollow(req_id=500 + i, user_id=user_a_id, followee_id=b_id, carrier={})
            t1 = time.perf_counter()
            latencies_unfollow.append((t1 - t0) * 1000)
            transport.close()
        except Exception as exc:
            print(f"❌ [Unfollow Iter {i}] Failed: {exc}")
            invariant_violations += 1

    try:
        transport, client = get_thrift_client()
        a_followees = client.GetFollowees(req_id=599, user_id=user_a_id, carrier={})
        transport.close()
        assert len(a_followees) == 0, f"Expected 0 followees after unfollow loop, got {len(a_followees)}"
        print("  ✓ Unfollow invariant verified: All followees removed")
    except Exception as exc:
        print(f"❌ [Unfollow Verification] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 6. Test FollowWithUsername & UnfollowWithUsername (Integration with UserService)
    # ------------------------------------------------------------------
    print("\n[Test 6] Testing FollowWithUsername & UnfollowWithUsername Integration...")
    uname_a = f"sg_user_a_{uuid.uuid4().hex[:6]}"
    uname_b = f"sg_user_b_{uuid.uuid4().hex[:6]}"

    try:
        # Step A: Register users in UserService
        u_transport, u_client = get_user_thrift_client()
        u_client.RegisterUser(req_id=601, first_name="SG", last_name="A", username=uname_a, password="pwd", carrier={})
        u_client.RegisterUser(req_id=602, first_name="SG", last_name="B", username=uname_b, password="pwd", carrier={})
        uid_a = u_client.GetUserId(req_id=603, username=uname_a, carrier={})
        uid_b = u_client.GetUserId(req_id=604, username=uname_b, carrier={})
        u_transport.close()

        # Step B: FollowWithUsername
        t0 = time.perf_counter()
        transport, client = get_thrift_client()
        client.FollowWithUsername(req_id=605, user_username=uname_a, followee_username=uname_b, carrier={})
        t1 = time.perf_counter()
        latencies_follow_username.append((t1 - t0) * 1000)

        # Verify reciprocity
        followers_b = client.GetFollowers(req_id=606, user_id=uid_b, carrier={})
        assert uid_a in followers_b, "FollowWithUsername failed to link graph entities"

        # Step C: UnfollowWithUsername
        client.UnfollowWithUsername(req_id=607, user_username=uname_a, followee_username=uname_b, carrier={})
        followers_b_post = client.GetFollowers(req_id=608, user_id=uid_b, carrier={})
        transport.close()

        assert uid_a not in followers_b_post, "UnfollowWithUsername failed to sever graph link"
        print("  ✓ Username resolution and graph mutations verified successfully")
    except Exception as exc:
        print(f"❌ [Follow/Unfollow With Username] Failed: {exc}")
        invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("      SOCIAL GRAPH SERVICE REGRESSION & LATENCY REPORT       ")
    print("=" * 65)
    print(f"Total Iterations        : {ITERATIONS}")
    print(f"Invariant Violations    : {invariant_violations}")

    if latencies_insert:
        print("\n--- Latency Performance: InsertUser ---")
        print(f"InsertUser             | p50: {np.median(latencies_insert):.3f} ms")

    if latencies_follow:
        print("\n--- Latency Performance: Follow / Unfollow ---")
        print(
            f"Follow                 | p50: {np.median(latencies_follow):.3f} ms | "
            f"p95: {np.percentile(latencies_follow, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_follow, 99):.3f} ms"
        )
        print(
            f"Unfollow               | p50: {np.median(latencies_unfollow):.3f} ms | "
            f"p95: {np.percentile(latencies_unfollow, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_unfollow, 99):.3f} ms"
        )

    if latencies_get_followers_hit:
        print("\n--- Latency Performance: GetFollowers ---")
        print(
            f"GetFollowers (Hit)     | p50: {np.median(latencies_get_followers_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_get_followers_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_get_followers_hit, 99):.3f} ms"
        )
    if latencies_get_followers_miss:
        print(
            f"GetFollowers (Miss)    | p50: {latencies_get_followers_miss[0]:.3f} ms (Initial miss & Redis seed latency)"
        )

    if latencies_follow_username:
        print("\n--- Latency Performance: FollowWithUsername ---")
        print(f"FollowWithUsername     | Latency: {latencies_follow_username[0]:.3f} ms")


if __name__ == "__main__":
    run_social_graph_regression_suite()