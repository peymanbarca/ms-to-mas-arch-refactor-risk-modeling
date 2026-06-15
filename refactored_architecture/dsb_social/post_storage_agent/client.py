# client.py
import sys
sys.path.insert(0, "gen_py")

from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol
from ms_baseline.dsb_social.gen_py.social_network import PostStorageService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import Post, Creator, UserMention, Url, PostType
import time

def make_client(host="127.0.0.1", port=9096):
    sock      = TSocket.TSocket(host, port)
    transport = TTransport.TFramedTransport(sock)
    protocol  = TBinaryProtocol.TBinaryProtocol(transport)
    client    = PostStorageService.Client(protocol)
    transport.open()
    return client, transport

def print_post(post):
    """Pretty print a Post object"""
    print(f"  post_id: {post.post_id}")
    print(f"  creator: {post.creator.user_id if post.creator else 'N/A'}")
    print(f"  text: {post.text}")
    print(f"  timestamp: {post.timestamp}")
    if post.user_mentions:
        print(f"  user_mentions: {len(post.user_mentions)} mentions")
    if post.urls:
        print(f"  urls: {', '.join([u.shortened_url for u in post.urls])}")
    print()

if __name__ == "__main__":
    client, transport = make_client()
    try:
        print("=== PostStorageService Client Demo ===\n")
        
        # Store some sample posts
        print("1. StorePost - Creating 3 sample posts")
        print("-" * 40)
        posts_to_store = []
        
        for i in range(3):
            post = Post(
                post_id=2000 + i,
                creator=Creator(user_id=100 + i),
                req_id=i,
                text=f"This is sample post #{i+1} with some content",
                user_mentions=[],
                media=[],
                urls=[
                    Url(
                        shortened_url=f"http://short.url/{i}",
                        expanded_url=f"http://example.com/page/{i}"
                    )
                ],
                timestamp=int(time.time()),
                post_type=PostType.POST
            )
            posts_to_store.append(post)
            
            client.StorePost(
                req_id=i,
                post=post,
                carrier={}
            )
            print(f"  Stored post_id={post.post_id}")
        
        print("\n2. ReadPost - Retrieving single post")
        print("-" * 40)
        a = input("Press Enter to read single post...")
        
        retrieved_post = client.ReadPost(req_id=1, post_id=2000, carrier={})
        print(f"Retrieved post_id={retrieved_post.post_id}:")
        print_post(retrieved_post)
        
        print("3. ReadPosts - Retrieving multiple posts")
        print("-" * 40)
        a = input("Press Enter to read posts batch...")

        post_ids = [2000, 2001, 2002]
        retrieved_posts = client.ReadPosts(req_id=2, post_ids=post_ids, carrier={})
        print(f"Retrieved {len(retrieved_posts)} posts:")
        for post in retrieved_posts:
            print(f"Retrieved post_id={post.post_id}:")
            print_post(post)
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        transport.close()
