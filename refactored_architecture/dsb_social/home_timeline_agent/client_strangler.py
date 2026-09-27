# client.py
import sys
import os
import random
import time

sys.path.insert(0, "gen_py")

from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol
from ms_baseline.dsb_social.gen_py.social_network import HomeTimelineService

# ---------------- STRANGLER CONFIG ----------------
STRANGLER_V2_RATE = int(os.environ.get("STRANGLER_V2_RATE", "90"))

HOST_V1 = os.environ.get("HOST_V1", "127.0.0.1")
PORT_V1 = int(os.environ.get("PORT_V1", 9099))

HOST_V2 = os.environ.get("HOST_V2", "127.0.0.1")
PORT_V2 = int(os.environ.get("PORT_V2", 10099))


def make_client():
    """
    Dynamically routes the Thrift client connection to V1 or V2 
    based on the configured STRANGLER_V2_RATE percentage.
    Returns (client, transport, version_tag).
    """
    if random.randint(1, 100) <= STRANGLER_V2_RATE:
        host, port, version = HOST_V2, PORT_V2, "V2"
    else:
        host, port, version = HOST_V1, PORT_V1, "V1"

    sock      = TSocket.TSocket(host, port)
    transport = TTransport.TFramedTransport(sock)
    protocol  = TBinaryProtocol.TBinaryProtocol(transport)
    client    = HomeTimelineService.Client(protocol)
    transport.open()
    return client, transport, version


def print_post(post):
    """Pretty print a Post object"""
    print(f"    post_id: {post.post_id}")
    print(f"    creator: {post.creator.user_id if post.creator else 'N/A'}")
    print(f"    text: {post.text}")
    print(f"    timestamp: {post.timestamp}")
    print()


if __name__ == "__main__":
    client, transport, active_version = make_client()
    try:
        print(f"=== HomeTimelineService Client Demo [{active_version}] (Strangler Rate: {STRANGLER_V2_RATE}% V2) ===\n")
        
        # Demo scenario: user_id=3 is the author
        # user_ids 1, 2 are followers (they will receive the posts in their home timeline)
        # user_id 2 will be mentioned
        
        author_id = 3
        current_timestamp = int(time.time() * 1000)  # milliseconds
        
        print("1. WriteHomeTimeline - Fan out posts to follower timelines")
        print("-" * 60)
        
        # Write 3 posts from author to their followers' home timelines
        for i in range(3):
            post_id = 1000 + i
            timestamp = current_timestamp - (i * 1000)  # stagger timestamps
            
            # Last post mentions user 2
            mentions = [2] if i == 2 else []
            
            client.WriteHomeTimeline(
                req_id=i,
                post_id=post_id,
                user_id=author_id,
                timestamp=timestamp,
                user_mentions_id=mentions,
                carrier={}
            )
            mention_str = f" (mentioning user 2)" if mentions else ""
            print(f"  req_id={i}  post_id={post_id}  author={author_id}{mention_str}")
        
        print("\n2. ReadHomeTimeline - Read timeline for follower user_id=1")
        print("-" * 60)
        
        # Read posts from the home timeline of user 1 (a follower)
        posts = client.ReadHomeTimeline(
            req_id=10,
            user_id=1,
            start=0,
            stop=10,
            carrier={}
        )
        
        if posts:
            print(f"  Retrieved {len(posts)} posts from home timeline of user 1:\n")
            for post in posts:
                print_post(post)
        else:
            print(f"  No posts found in timeline for user 1 (empty timeline)")
        
        print("3. ReadHomeTimeline - Read timeline for mentioned user_id=2")
        print("-" * 60)
        
        # Read posts from the home timeline of user 2 (mentioned in last post)
        posts = client.ReadHomeTimeline(
            req_id=11,
            user_id=2,
            start=0,
            stop=10,
            carrier={}
        )
        
        if posts:
            print(f"  Retrieved {len(posts)} posts from home timeline of user 2:\n")
            for post in posts:
                print_post(post)
        else:
            print(f"  No posts found in timeline for user 2 (cold start)")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        transport.close()