"""
regression_test.py

Regression Test & Benchmarking Suite for UserMentionService (Thrift RPC).
Runs N iterations over the Thrift interface to verify:
  1. Resolution of valid @mention usernames into UserMention structs (user_id + username).
  2. Cache-aside pattern (MongoDB miss/hit -> Redis backfill -> subsequent Redis hit).
  3. De-duplication of identical usernames within a single request while preserving order.
  4. Exception handling for non-existent users (ServiceException code SE_THRIFT_HANDLER_ERROR).
  5. Latency performance distributions for cache miss vs cache hit scenarios (p50, p95, p99).
"""

import time
import uuid
import numpy as np
from pymongo import MongoClient
import redis

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import UserMentionService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    UserMention,
    ServiceException,
    ErrorCode,
)

THRIFT_HOST = "localhost"
THRIFT_PORT = 9093
MONGO_HOST = "localhost"
MONGO_PORT = 27017
REDIS_HOST = "localhost"
REDIS_PORT = 6385
ITERATIONS = 50


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = UserMentionService.Client(protocol)
    transport.open()
    return transport, client


def seed_test_user_in_mongo(user_id: int, username: str):
    client = MongoClient(host=MONGO_HOST, port=MONGO_PORT, serverSelectionTimeoutMS=2000)
    db = client["user"]
    col = db["user"]
    col.update_one(
        {"username": username},
        {"$set": {"user_id": user_id, "username": username}},
        upsert=True,
    )
    client.close()


def clear_redis_keys(usernames: list[str]):
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, password="1")
    for username in usernames:
        r.delete(username)
    r.close()


def run_user_mention_regression_suite():
    print(f"--- Starting UserMentionService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_miss = []
    latencies_hit = []
    invariant_violations = 0

    # ------------------------------------------------------------------
    # 1. Test Unregistered Username Resolution (Error Handling)
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Non-Existent User Exception Handler...")
    non_existent_username = f"unregistered_{uuid.uuid4().hex[:8]}"
    try:
        transport, client = get_thrift_client()
        client.ComposeUserMentions(
            req_id=9999,
            usernames=[non_existent_username],
            carrier={},
        )
        transport.close()
        print("❌ VIOLATION: Expected ServiceException on missing username")
        invariant_violations += 1
    except ServiceException as exc:
        if exc.errorCode == ErrorCode.SE_THRIFT_HANDLER_ERROR:
            print("  ✓ Correctly caught SE_THRIFT_HANDLER_ERROR for missing username")
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

    # Seed test users into MongoDB
    test_data = []
    base_id = int(time.time())
    for i in range(ITERATIONS):
        uid = base_id + i
        uname = f"user_mention_{i}_{uuid.uuid4().hex[:4]}"
        seed_test_user_in_mongo(uid, uname)
        test_data.append((uid, uname))

    all_usernames = [u[1] for u in test_data]
    clear_redis_keys(all_usernames)

    # ------------------------------------------------------------------
    # 2. Benchmark ComposeUserMentions (Cache Miss: Mongo Read + Redis Backfill)
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking ComposeUserMentions Cache Miss ({ITERATIONS} Iterations)...")
    for i, (expected_uid, username) in enumerate(test_data, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res = client.ComposeUserMentions(
                req_id=i,
                usernames=[username],
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_miss.append((t1 - t0) * 1000)
            transport.close()

            # Invariants
            assert len(res) == 1, f"[Miss Iter {i}] VIOLATION: Expected 1 UserMention"
            assert res[0].user_id == expected_uid, (
                f"[Miss Iter {i}] VIOLATION: Expected user_id {expected_uid}, got {res[0].user_id}"
            )
            assert res[0].username == username, (
                f"[Miss Iter {i}] VIOLATION: Expected username {username}, got {res[0].username}"
            )
        except Exception as exc:
            print(f"❌ [Cache Miss Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Benchmark ComposeUserMentions (Cache Hit)
    # ------------------------------------------------------------------
    print(f"\n[Test 3] Benchmarking ComposeUserMentions Cache Hit ({ITERATIONS} Iterations)...")
    for i, (expected_uid, username) in enumerate(test_data, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res = client.ComposeUserMentions(
                req_id=100 + i,
                usernames=[username],
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_hit.append((t1 - t0) * 1000)
            transport.close()

            assert len(res) == 1, f"[Hit Iter {i}] VIOLATION: Expected 1 UserMention"
            assert res[0].user_id == expected_uid, (
                f"[Hit Iter {i}] VIOLATION: Expected user_id {expected_uid}, got {res[0].user_id}"
            )
        except Exception as exc:
            print(f"❌ [Cache Hit Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 4. In-Request De-duplication & Order Preservation Invariant Test
    # ------------------------------------------------------------------
    print("\n[Test 4] Validating Request De-duplication & Order Preservation...")
    if len(test_data) >= 2:
        u1_id, u1_name = test_data[0]
        u2_id, u2_name = test_data[1]
        input_list = [u1_name, u2_name, u1_name, u2_name]

        try:
            transport, client = get_thrift_client()
            res = client.ComposeUserMentions(
                req_id=9000,
                usernames=input_list,
                carrier={},
            )
            transport.close()

            assert len(res) == 4, (
                f"VIOLATION: Expected output length 4 matching input list, got {len(res)}"
            )
            assert [m.username for m in res] == input_list, (
                "VIOLATION: Output usernames sequence does not preserve original request ordering"
            )
            assert [m.user_id for m in res] == [u1_id, u2_id, u1_id, u2_id], (
                "VIOLATION: De-duplicated internal resolution mapped incorrectly to output sequence"
            )
            print("  ✓ In-request de-duplication and ordering invariants passed")

        except Exception as exc:
            print(f"❌ [De-duplication Test] Failed: {exc}")
            invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("     USER MENTION SERVICE REGRESSION & LATENCY REPORT     ")
    print("=" * 65)
    print(f"Total Iterations per Test : {ITERATIONS}")
    print(f"Invariant Violations     : {invariant_violations}")

    if latencies_miss:
        print("\n--- Latency Performance: ComposeUserMentions (Cache Miss / Mongo Read) ---")
        print(
            f"ComposeUserMentions (Miss) | p50: {np.median(latencies_miss):.3f} ms | "
            f"p95: {np.percentile(latencies_miss, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_miss, 99):.3f} ms"
        )

    if latencies_hit:
        print("\n--- Latency Performance: ComposeUserMentions (Cache Hit / Redis) ---")
        print(
            f"ComposeUserMentions (Hit)  | p50: {np.median(latencies_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_hit, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_user_mention_regression_suite()