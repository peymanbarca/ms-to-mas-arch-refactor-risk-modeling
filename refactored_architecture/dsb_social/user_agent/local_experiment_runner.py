"""
regression_test.py

Regression Test & Benchmarking Suite for UserService (Thrift RPC).
Runs N iterations over the Thrift interface to verify:
  1. User Registration (RegisterUser auto ID generation & RegisterUserWithId explicit assignment).
  2. Duplicate Registration Prevention (DuplicateKeyError -> SE_THRIFT_HANDLER_ERROR).
  3. Authentication & JWT Token Issuance via Login (and validation using jwt_helper).
  4. Invalid Credential Rejection (SE_UNAUTHORIZED on wrong password, SE_THRIFT_HANDLER_ERROR on missing user).
  5. Username-to-ID Resolution via GetUserId & Creator composition (ComposeCreatorWithUsername / ComposeCreatorWithUserId).
  6. Two-tier Cache behavior (username -> user_id, user_id -> JSON document in Redis).
  7. Latency distributions for Registration, Login, and Username Resolution (p50, p95, p99).
"""

import time
import uuid
import numpy as np
import redis

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import UserService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Creator,
    ServiceException,
    ErrorCode,
)
from refactored_architecture.dsb_social.user_agent.jwt_helper import decode_token

THRIFT_HOST = "localhost"
THRIFT_PORT = 9094
REDIS_HOST = "localhost"
REDIS_PORT = 6385
JWT_SECRET = "secret"  # Default secret from service-config.json

ITERATIONS = 50


def get_thrift_client():
    socket = TSocket.TSocket(THRIFT_HOST, THRIFT_PORT)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = UserService.Client(protocol)
    transport.open()
    return transport, client


def clear_redis_user_cache(usernames: list[str], user_ids: list[int]):
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, password="1")
    for uname in usernames:
        r.delete(f"username:{uname}")
    for uid in user_ids:
        r.delete(f"userid:{uid}")
    r.close()


def run_user_regression_suite():
    print(f"--- Starting UserService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_register = []
    latencies_login = []
    latencies_get_id_hit = []
    latencies_get_id_miss = []
    invariant_violations = 0

    # Test data generation
    user_credentials = []
    for i in range(ITERATIONS):
        uname = f"usr_{i}_{uuid.uuid4().hex[:6]}"
        pwd = f"pass_{uuid.uuid4().hex[:8]}"
        user_credentials.append((uname, pwd, f"First_{i}", f"Last_{i}"))

    # ------------------------------------------------------------------
    # 1. Test Unregistered User Exceptions (Login & Resolution)
    # ------------------------------------------------------------------
    print("\n[Test 1] Validating Non-Existent User Exception Handlers...")
    non_existent = f"ghost_{uuid.uuid4().hex[:8]}"
    
    # Check Login
    try:
        transport, client = get_thrift_client()
        client.Login(req_id=9001, username=non_existent, password="dummy_password", carrier={})
        transport.close()
        print("❌ VIOLATION: Expected ServiceException on missing user login")
        invariant_violations += 1
    except ServiceException as exc:
        if exc.errorCode == ErrorCode.SE_THRIFT_HANDLER_ERROR:
            print("  ✓ Correctly caught SE_THRIFT_HANDLER_ERROR for non-existent login")
        else:
            print(f"❌ VIOLATION: Unexpected errorCode: {exc.errorCode}")
            invariant_violations += 1
        try:
            transport.close()
        except Exception:
            pass

    # Check GetUserId
    try:
        transport, client = get_thrift_client()
        client.GetUserId(req_id=9002, username=non_existent, carrier={})
        transport.close()
        print("❌ VIOLATION: Expected ServiceException on missing user resolution")
        invariant_violations += 1
    except ServiceException as exc:
        if exc.errorCode == ErrorCode.SE_THRIFT_HANDLER_ERROR:
            print("  ✓ Correctly caught SE_THRIFT_HANDLER_ERROR for non-existent GetUserId")
        else:
            print(f"❌ VIOLATION: Unexpected errorCode: {exc.errorCode}")
            invariant_violations += 1
        try:
            transport.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 2. Benchmark RegisterUser (Auto ID Generation) & Duplicate Prevention
    # ------------------------------------------------------------------
    print(f"\n[Test 2] Benchmarking RegisterUser ({ITERATIONS} Iterations) & Duplicate Check...")
    for i, (uname, pwd, fname, lname) in enumerate(user_credentials, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            client.RegisterUser(
                req_id=i,
                first_name=fname,
                last_name=lname,
                username=uname,
                password=pwd,
                carrier={},
            )
            t1 = time.perf_counter()
            latencies_register.append((t1 - t0) * 1000)

            # Check Duplicate Registration on first item
            if i == 1:
                try:
                    client.RegisterUser(
                        req_id=999,
                        first_name=fname,
                        last_name=lname,
                        username=uname,
                        password=pwd,
                        carrier={},
                    )
                    print("❌ VIOLATION: Allowed duplicate username registration")
                    invariant_violations += 1
                except ServiceException as exc:
                    if exc.errorCode == ErrorCode.SE_THRIFT_HANDLER_ERROR:
                        print("  ✓ Correctly rejected duplicate username registration")
                    else:
                        print(f"❌ VIOLATION: Unexpected errorCode on duplicate: {exc.errorCode}")
                        invariant_violations += 1

            transport.close()
        except Exception as exc:
            print(f"❌ [RegisterUser Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 3. Test RegisterUserWithId (Explicit ID Assignment)
    # ------------------------------------------------------------------
    print("\n[Test 3] Validating RegisterUserWithId...")
    explicit_uname = f"explicit_{uuid.uuid4().hex[:6]}"
    explicit_uid = 888000 + int(time.time() % 100000)
    try:
        transport, client = get_thrift_client()
        client.RegisterUserWithId(
            req_id=8001,
            first_name="Explicit",
            last_name="User",
            username=explicit_uname,
            password="password123",
            user_id=explicit_uid,
            carrier={},
        )
        resolved_uid = client.GetUserId(req_id=8002, username=explicit_uname, carrier={})
        transport.close()

        assert resolved_uid == explicit_uid, (
            f"VIOLATION: Explicit ID mismatch. Expected {explicit_uid}, got {resolved_uid}"
        )
        print("  ✓ RegisterUserWithId registered explicit ID correctly")
    except Exception as exc:
        print(f"❌ [RegisterUserWithId] Failed: {exc}")
        invariant_violations += 1

    # ------------------------------------------------------------------
    # 4. Benchmark Login & Validating JWT Tokens / Incorrect Passwords
    # ------------------------------------------------------------------
    print(f"\n[Test 4] Benchmarking Login ({ITERATIONS} Iterations) & JWT Validation...")
    resolved_uids = {}

    for i, (uname, pwd, fname, lname) in enumerate(user_credentials, 1):
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            jwt_token = client.Login(req_id=100 + i, username=uname, password=pwd, carrier={})
            t1 = time.perf_counter()
            latencies_login.append((t1 - t0) * 1000)

            # Validate JWT Payload
            payload = decode_token(jwt_token, JWT_SECRET)
            assert payload["username"] == uname, f"[Iter {i}] JWT username payload mismatch"
            assert isinstance(payload["user_id"], int), f"[Iter {i}] JWT user_id invalid"
            resolved_uids[uname] = payload["user_id"]

            # Test Wrong Password (SE_UNAUTHORIZED) on first item
            if i == 1:
                try:
                    client.Login(req_id=998, username=uname, password="wrong_password", carrier={})
                    print("❌ VIOLATION: Login succeeded with wrong password")
                    invariant_violations += 1
                except ServiceException as exc:
                    if exc.errorCode == ErrorCode.SE_UNAUTHORIZED:
                        print("  ✓ Correctly caught SE_UNAUTHORIZED on invalid password")
                    else:
                        print(f"❌ VIOLATION: Unexpected errorCode on wrong password: {exc.errorCode}")
                        invariant_violations += 1

            transport.close()
        except Exception as exc:
            print(f"❌ [Login Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 5. Benchmark Resolution & Creator Composition (Cache Hit)
    # ------------------------------------------------------------------
    print(f"\n[Test 5] Benchmarking GetUserId & Creator Composition (Cache HIT - {ITERATIONS} Iterations)...")
    for i, (uname, pwd, fname, lname) in enumerate(user_credentials, 1):
        expected_uid = resolved_uids.get(uname)
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            uid = client.GetUserId(req_id=200 + i, username=uname, carrier={})
            t1 = time.perf_counter()
            latencies_get_id_hit.append((t1 - t0) * 1000)

            assert uid == expected_uid, f"[GetUserId Hit Iter {i}] ID mismatch"

            # Validate ComposeCreatorWithUsername
            creator_uname = client.ComposeCreatorWithUsername(req_id=250 + i, username=uname, carrier={})
            assert creator_uname.user_id == expected_uid and creator_uname.username == uname

            # Validate ComposeCreatorWithUserId (Pass-through logic)
            creator_uid = client.ComposeCreatorWithUserId(req_id=280 + i, user_id=expected_uid, username=uname, carrier={})
            assert creator_uid.user_id == expected_uid and creator_uid.username == uname

            transport.close()
        except Exception as exc:
            print(f"❌ [Resolution Hit Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 6. Benchmark Resolution (Cache Miss / Mongo Fallback)
    # ------------------------------------------------------------------
    print(f"\n[Test 6] Benchmarking GetUserId (Cache MISS - {ITERATIONS} Iterations)...")
    clear_redis_user_cache(list(resolved_uids.keys()), list(resolved_uids.values()))

    for i, (uname, pwd, fname, lname) in enumerate(user_credentials, 1):
        expected_uid = resolved_uids.get(uname)
        t0 = time.perf_counter()
        try:
            transport, client = get_thrift_client()
            uid = client.GetUserId(req_id=300 + i, username=uname, carrier={})
            t1 = time.perf_counter()
            latencies_get_id_miss.append((t1 - t0) * 1000)
            transport.close()

            assert uid == expected_uid, f"[GetUserId Miss Iter {i}] ID mismatch"
        except Exception as exc:
            print(f"❌ [GetUserId Miss Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("          USER SERVICE REGRESSION & LATENCY REPORT          ")
    print("=" * 65)
    print(f"Total Iterations per Test : {ITERATIONS}")
    print(f"Invariant Violations     : {invariant_violations}")

    if latencies_register:
        print("\n--- Latency Performance: RegisterUser ---")
        print(
            f"RegisterUser           | p50: {np.median(latencies_register):.3f} ms | "
            f"p95: {np.percentile(latencies_register, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_register, 99):.3f} ms"
        )

    if latencies_login:
        print("\n--- Latency Performance: Login ---")
        print(
            f"Login                  | p50: {np.median(latencies_login):.3f} ms | "
            f"p95: {np.percentile(latencies_login, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_login, 99):.3f} ms"
        )

    if latencies_get_id_hit:
        print("\n--- Latency Performance: GetUserId / Resolution ---")
        print(
            f"GetUserId (Cache Hit)  | p50: {np.median(latencies_get_id_hit):.3f} ms | "
            f"p95: {np.percentile(latencies_get_id_hit, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_get_id_hit, 99):.3f} ms"
        )
        print(
            f"GetUserId (Cache Miss) | p50: {np.median(latencies_get_id_miss):.3f} ms | "
            f"p95: {np.percentile(latencies_get_id_miss, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_get_id_miss, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_user_regression_suite()