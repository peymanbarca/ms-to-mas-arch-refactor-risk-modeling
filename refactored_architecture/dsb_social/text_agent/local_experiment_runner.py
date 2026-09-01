"""
text_service/tests/regression_test.py

Regression Test & Benchmarking Suite for TextService (Thrift RPC).
Runs N iterations over the Thrift interface to verify:
  1. Regex parsing & parallel fan-out to UrlShortenService and UserMentionService.
  2. URL replacement in text with returned short URLs.
  3. Proper aggregation of UserMention and Url struct lists in TextServiceReturn.
  4. Error propagation from downstream services (e.g., unregistered @mentions raising ServiceException).
  5. Latency distribution (p50, p95, p99) under concurrent downstream fan-out execution.
"""

import time
import uuid
import numpy as np
from pymongo import MongoClient
import redis

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import TextService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    TextServiceReturn,
    ServiceException,
    ErrorCode,
)

THRIFT_HOST = "localhost"
THRIFT_PORT = 9095

# Downstream DB connections to pre-seed valid test users for UserMentionService
MONGO_HOST = "localhost"
MONGO_PORT = 27017
REDIS_HOST = "localhost"
REDIS_PORT = 6385

ITERATIONS = 50


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = TextService.Client(protocol)
    transport.open()
    return transport, client


def seed_test_user_in_mongo(user_id: int, username: str):
    """Seed user into the shared MongoDB 'user' collection so UserMentionService can resolve it."""
    client = MongoClient(host=MONGO_HOST, port=MONGO_PORT, serverSelectionTimeoutMS=2000)
    db = client["user"]
    col = db["user"]
    col.update_one(
        {"username": username},
        {"$set": {"user_id": user_id, "username": username}},
        upsert=True,
    )
    client.close()


def clear_mention_cache(username: str):
    """Clear Redis cache key for UserMentionService."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, password="1")
    r.delete(username)
    r.close()


def run_text_regression_suite():
    print(f"--- Starting TextService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_compose_text = []
    latencies_no_fanout = []
    invariant_violations = 0

    # Pre-seed a valid user for @mention testing
    valid_uid = int(time.time())
    valid_uname = f"text_svc_user_{uuid.uuid4().hex[:6]}"
    seed_test_user_in_mongo(valid_uid, valid_uname)
    clear_mention_cache(valid_uname)

    # ------------------------------------------------------------------
    # 1. Test Downstream Error Propagation (Unregistered Mention)
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Downstream Error Propagation (Invalid Mention)...")
    invalid_uname = f"ghost_user_{uuid.uuid4().hex[:8]}"
    text_with_bad_mention = f"Hello @{invalid_uname} check this out https://example.com/test"

    try:
        transport, client = get_thrift_client()
        client.ComposeText(
            req_id=9999,
            text=text_with_bad_mention,
            carrier={},
        )
        transport.close()
        print("❌ VIOLATION: Expected ServiceException when mentioning unregistered user")
        invariant_violations += 1
    except ServiceException as exc:
        print(f"  ✓ Correctly caught propagated ServiceException (errorCode={exc.errorCode})")
        try:
            transport.close()
        except Exception:
            pass
    except Exception as exc:
        print(f"❌ VIOLATION: Unexpected exception type: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 2. Benchmark ComposeText with Full Fan-Out (URLs + Mentions)
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking ComposeText with Downstream Parallel Fan-Out ({ITERATIONS} Iterations)...")

    for i in range(1, ITERATIONS + 1):
        target_url = f"http://example.org/article/{i}/{uuid.uuid4().hex[:4]}"
        raw_text = f"Hey @{valid_uname}, check out this paper: {target_url} #research"

        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res: TextServiceReturn = client.ComposeText(
                req_id=i,
                text=raw_text,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_compose_text.append((t1 - t0) * 1000)
            transport.close()

            # Invariant 1: Mentions returned and matched
            assert len(res.user_mentions) == 1, (
                f"[Iter {i}] VIOLATION: Expected 1 UserMention, got {len(res.user_mentions)}"
            )
            assert res.user_mentions[0].username == valid_uname, (
                f"[Iter {i}] VIOLATION: Expected mention username {valid_uname}, got {res.user_mentions[0].username}"
            )
            assert res.user_mentions[0].user_id == valid_uid, (
                f"[Iter {i}] VIOLATION: Expected mention user_id {valid_uid}, got {res.user_mentions[0].user_id}"
            )

            # Invariant 2: URLs returned and replaced in modified text
            assert len(res.urls) == 1, (
                f"[Iter {i}] VIOLATION: Expected 1 Url struct, got {len(res.urls)}"
            )
            assert res.urls[0].expanded_url == target_url, (
                f"[Iter {i}] VIOLATION: Expanded URL mismatch"
            )

            short_url = res.urls[0].shortened_url
            assert short_url in res.text, (
                f"[Iter {i}] VIOLATION: Shortened URL '{short_url}' not found in modified text: '{res.text}'"
            )
            assert target_url not in res.text, (
                f"[Iter {i}] VIOLATION: Original URL '{target_url}' still present in modified text"
            )

        except Exception as exc:
            print(f"❌ [ComposeText Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Benchmark Plain Text (No URLs, No Mentions - Fast Path)
    # ------------------------------------------------------------------
    print(f"\n[Test 3] Benchmarking Plain Text Fast Path ({ITERATIONS} Iterations)...")

    for i in range(1, ITERATIONS + 1):
        plain_text = f"Just a simple status update with no links or mentions #{i}"

        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            res: TextServiceReturn = client.ComposeText(
                req_id=100 + i,
                text=plain_text,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_no_fanout.append((t1 - t0) * 1000)
            transport.close()

            assert res.text == plain_text, f"[Plain Iter {i}] VIOLATION: Modified text changed unexpectedly"
            assert len(res.urls) == 0, f"[Plain Iter {i}] VIOLATION: Expected 0 URLs"
            assert len(res.user_mentions) == 0, f"[Plain Iter {i}] VIOLATION: Expected 0 Mentions"

        except Exception as exc:
            print(f"❌ [Plain Text Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("        TEXT SERVICE REGRESSION & LATENCY REPORT        ")
    print("=" * 65)
    print(f"Total Iterations per Test : {ITERATIONS}")
    print(f"Invariant Violations     : {invariant_violations}")

    if latencies_compose_text:
        print("\n--- Latency Performance: ComposeText (Parallel Fan-Out) ---")
        print(
            f"ComposeText (Fan-Out) | p50: {np.median(latencies_compose_text):.3f} ms | "
            f"p95: {np.percentile(latencies_compose_text, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_compose_text, 99):.3f} ms"
        )

    if latencies_no_fanout:
        print("\n--- Latency Performance: ComposeText (Fast Path / Plain Text) ---")
        print(
            f"ComposeText (Fast)    | p50: {np.median(latencies_no_fanout):.3f} ms | "
            f"p95: {np.percentile(latencies_no_fanout, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_no_fanout, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_text_regression_suite()