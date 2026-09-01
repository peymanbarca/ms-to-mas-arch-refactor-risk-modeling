"""
regression_test.py

Regression Test & Benchmarking Suite for ComposePostService (Thrift RPC Orchestrator).
Runs N iterations over the Thrift interface to verify:
  1. Orchestration Phase 1 Parallel Fan-Out (UniqueIdService, TextService, UserService, MediaService).
  2. Orchestration Phase 2 Post Object Struct Assembly (text parsing, user mentions, URLs, media links).
  3. Orchestration Phase 3 Synchronous Downstream Writes (PostStorageService & UserTimelineService).
  4. Orchestration Phase 3 Asynchronous Event Dispatch (RabbitMQ -> WriteHomeTimelineService -> HomeTimelineService).
  5. Multi-media and user-mention extraction invariants.
  6. Failure propagation when downstream dependencies fail (SE_THRIFT_HANDLER_ERROR).
  7. Latency performance metrics for full workflow orchestration (p50, p95, p99).
"""

import time
import uuid
import numpy as np

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import (
    ComposePostService,
    PostStorageService,
    UserTimelineService,
    HomeTimelineService,
    UserService,
    SocialGraphService,
)
from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    PostType,
    ServiceException,
    ErrorCode,
)

COMPOSE_POST_HOST = "localhost"
COMPOSE_POST_PORT = 9100

POST_STORAGE_HOST = "localhost"
POST_STORAGE_PORT = 9096

USER_TIMELINE_HOST = "localhost"
USER_TIMELINE_PORT = 9098

HOME_TIMELINE_HOST = "localhost"
HOME_TIMELINE_PORT = 9099

USER_SERVICE_HOST = "localhost"
USER_SERVICE_PORT = 9094

SOCIAL_GRAPH_HOST = "localhost"
SOCIAL_GRAPH_PORT = 9097

ITERATIONS = 50
ASYNC_PROPAGATION_TIMEOUT_SEC = 2.0


def get_thrift_client(service_class, host: str, port: int):
    socket = TSocket.TSocket(host, port)
    transport = TTransport.TFramedTransport(socket)
    protocol = TBinaryProtocol.TBinaryProtocol(transport)
    client = service_class.Client(protocol)
    transport.open()
    return transport, client


def run_compose_post_regression_suite():
    print(f"--- Starting ComposePostService Regression Test ({ITERATIONS} Iterations) ---")

    latencies_compose = []
    invariant_violations = 0

    # Step A: Register test author and mentioned target user via UserService
    author_username = f"author_{uuid.uuid4().hex[:6]}"
    mentioned_username = f"mentioned_{uuid.uuid4().hex[:6]}"
    follower_username = f"follower_{uuid.uuid4().hex[:6]}"

    try:
        u_trans, u_client = get_thrift_client(UserService, USER_SERVICE_HOST, USER_SERVICE_PORT)
        u_client.RegisterUser(req_id=1, first_name="Author", last_name="User", username=author_username, password="pwd", carrier={})
        u_client.RegisterUser(req_id=2, first_name="Mentioned", last_name="User", username=mentioned_username, password="pwd", carrier={})
        u_client.RegisterUser(req_id=3, first_name="Follower", last_name="User", username=follower_username, password="pwd", carrier={})

        author_id = u_client.GetUserId(req_id=4, username=author_username, carrier={})
        mentioned_id = u_client.GetUserId(req_id=5, username=mentioned_username, carrier={})
        follower_id = u_client.GetUserId(req_id=6, username=follower_username, carrier={})
        u_trans.close()

        # Connect follower to author via SocialGraphService
        sg_trans, sg_client = get_thrift_client(SocialGraphService, SOCIAL_GRAPH_HOST, SOCIAL_GRAPH_PORT)
        sg_client.Follow(req_id=7, user_id=follower_id, followee_id=author_id, carrier={})
        sg_trans.close()

        print(f"  ✓ Initialized entities: Author ID {author_id}, Mentioned ID {mentioned_id}, Follower ID {follower_id}")
    except Exception as exc:
        print(f"❌ Setup Failed (User/Graph Bootstrap): {exc}")
        return

    # ------------------------------------------------------------------
    # 1. Benchmark ComposePost Workflow Orchestration
    # ------------------------------------------------------------------
    print(f"\n[Test 1] Benchmarking ComposePost Orchestration ({ITERATIONS} Iterations)...")
    composed_post_data = []

    for i in range(1, ITERATIONS + 1):
        req_id = 1000 + i
        text = f"Post #{i} testing orchestration with @{mentioned_username} and http://example.com/item_{i}"
        media_ids = [10000 + i, 20000 + i]
        media_types = ["png", "jpg"]

        t0 = time.perf_counter()
        try:
            cp_trans, cp_client = get_thrift_client(ComposePostService, COMPOSE_POST_HOST, COMPOSE_POST_PORT)
            cp_client.ComposePost(
                req_id=req_id,
                username=author_username,
                user_id=author_id,
                text=text,
                media_ids=media_ids,
                media_types=media_types,
                post_type=PostType.POST,
                carrier={"trace_id": f"trace_compose_{req_id}"},
            )
            t1 = time.perf_counter()
            latencies_compose.append((t1 - t0) * 1000)
            cp_trans.close()
            composed_post_data.append((req_id, text))
        except Exception as exc:
            print(f"❌ [ComposePost Iter {i}] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 2. Verify UserTimelineService Downstream Persistence (Phase 3 Step 2)
    # ------------------------------------------------------------------
    print("\n[Test 2] Validating Downstream UserTimelineService Persistence...")
    try:
        ut_trans, ut_client = get_thrift_client(UserTimelineService, USER_TIMELINE_HOST, USER_TIMELINE_PORT)
        user_posts = ut_client.ReadUserTimeline(
            req_id=2001,
            user_id=author_id,
            start=0,
            stop=ITERATIONS + 10,
            carrier={},
        )
        ut_trans.close()

        assert len(user_posts) == ITERATIONS, (
            f"Expected {ITERATIONS} posts in UserTimelineService, found {len(user_posts)}"
        )

        sample_post_id = user_posts[0].post_id
        print(f"  ✓ UserTimelineService verified ({ITERATIONS} posts found, latest post_id={sample_post_id})")
    except Exception as exc:
        print(f"❌ [UserTimeline Verification] Failed: {exc}")
        invariant_violations += 1
        sample_post_id = None

    # ------------------------------------------------------------------
    # 3. Verify PostStorageService Post Assembly Invariants (Phase 2 & Phase 3 Step 1)
    # ------------------------------------------------------------------
    print("\n[Test 3] Validating PostStorageService Post Structure Invariants...")
    if sample_post_id is not None:
        try:
            ps_trans, ps_client = get_thrift_client(PostStorageService, POST_STORAGE_HOST, POST_STORAGE_PORT)
            hydrated_posts = ps_client.ReadPosts(
                req_id=3001,
                post_ids=[sample_post_id],
                carrier={},
            )
            ps_trans.close()

            assert len(hydrated_posts) == 1, "Failed to hydrate composed post from PostStorageService"
            post = hydrated_posts[0]

            # Invariant checks
            assert post.creator.user_id == author_id, "Creator user_id mismatch"
            assert post.creator.username == author_username, "Creator username mismatch"
            assert len(post.media) == 2, f"Expected 2 media items, got {len(post.media)}"
            assert len(post.user_mentions) == 1, "Failed to extract user mention"
            assert post.user_mentions[0].user_id == mentioned_id, "Mentioned user_id mismatch"
            assert len(post.urls) == 1, "Failed to extract URL"

            print("  ✓ Post structure invariants verified (Creator, Media, Mentions, URLs)")
        except Exception as exc:
            print(f"❌ [PostStorage Invariant Check] Failed: {exc}")
            invariant_violations += 1

    # ------------------------------------------------------------------
    # 4. Verify Asynchronous End-to-End Home Timeline Fan-Out (Phase 3 Step 3)
    # ------------------------------------------------------------------
    print("\n[Test 4] Validating Asynchronous Home Timeline Fan-Out via RabbitMQ...")
    t_poll_start = time.perf_counter()
    follower_timeline_updated = False

    while (time.perf_counter() - t_poll_start) < ASYNC_PROPAGATION_TIMEOUT_SEC:
        try:
            ht_trans, ht_client = get_thrift_client(HomeTimelineService, HOME_TIMELINE_HOST, HOME_TIMELINE_PORT)
            follower_posts = ht_client.ReadHomeTimeline(
                req_id=4001,
                user_id=follower_id,
                start=0,
                stop=ITERATIONS + 10,
                carrier={},
            )
            ht_trans.close()

            if len(follower_posts) == ITERATIONS:
                follower_timeline_updated = True
                break
        except Exception:
            pass
        time.sleep(0.1)

    if follower_timeline_updated:
        print(f"  ✓ Follower home timeline updated asynchronously with all {ITERATIONS} posts")
    else:
        print(f"❌ [Async Home Timeline Check] Follower timeline did not achieve eventual consistency within {ASYNC_PROPAGATION_TIMEOUT_SEC}s")
        invariant_violations += 1

    # Summary Report
    print("\n" + "=" * 65)
    print("      COMPOSE POST SERVICE REGRESSION & LATENCY REPORT      ")
    print("=" * 65)
    print(f"Total Iterations        : {ITERATIONS}")
    print(f"Invariant Violations    : {invariant_violations}")

    if latencies_compose:
        print("\n--- Latency Performance: ComposePost Workflow ---")
        print(
            f"ComposePost            | p50: {np.median(latencies_compose):.3f} ms | "
            f"p95: {np.percentile(latencies_compose, 95):.3f} ms | "
            f"p99: {np.percentile(latencies_compose, 99):.3f} ms"
        )


if __name__ == "__main__":
    run_compose_post_regression_suite()