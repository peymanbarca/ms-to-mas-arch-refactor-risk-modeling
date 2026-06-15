"""
post_serializer.py — Post struct ↔ dict (MongoDB/Redis) conversion.

The C++ PostStorageHandler stores Post objects as BSON documents in MongoDB
and serialises them to JSON strings for Memcached (Redis in our port).

MongoDB document schema (mirrors C++ BSON construction):
  {
    "post_id":      i64,
    "creator": {
        "user_id":  i64,
        "username": str
    },
    "req_id":       i64,
    "text":         str,
    "user_mentions": [
        {"user_id": i64, "username": str}, ...
    ],
    "media": [
        {"media_id": i64, "media_type": str}, ...
    ],
    "urls": [
        {"shortened_url": str, "expanded_url": str}, ...
    ],
    "timestamp":    i64,
    "post_type":    i32   (PostType enum value)
  }

Redis key  = str(post_id)
Redis value = JSON string of the above dict (no _id field).
"""

import json

from ms_baseline.dsb_social.gen_py.social_network.ttypes import (
    Post, Creator, UserMention, Media, Url, PostType,
)


# ---------------------------------------------------------------------------
# Thrift struct → dict  (for MongoDB insert and Redis cache)
# ---------------------------------------------------------------------------

def post_to_dict(post: Post) -> dict:
    """Serialise a Post struct to a plain dict suitable for MongoDB / JSON."""
    return {
        "post_id":   post.post_id,
        "creator": {
            "user_id":  post.creator.user_id,
            "username": post.creator.username,
        },
        "req_id":    post.req_id,
        "text":      post.text,
        "user_mentions": [
            {"user_id": m.user_id, "username": m.username}
            for m in (post.user_mentions or [])
        ],
        "media": [
            {"media_id": m.media_id, "media_type": m.media_type}
            for m in (post.media or [])
        ],
        "urls": [
            {"shortened_url": u.shortened_url, "expanded_url": u.expanded_url}
            for u in (post.urls or [])
        ],
        "timestamp": post.timestamp,
        "post_type": post.post_type,
    }


# ---------------------------------------------------------------------------
# dict → Thrift struct  (from MongoDB doc or Redis JSON)
# ---------------------------------------------------------------------------

def dict_to_post(d: dict) -> Post:
    """Deserialise a dict (from MongoDB or Redis) back to a Post struct."""
    creator_d = d.get("creator", {})
    return Post(
        post_id=int(d["post_id"]),
        creator=Creator(
            user_id=int(creator_d["user_id"]),
            username=creator_d["username"],
        ),
        req_id=int(d.get("req_id", 0)),
        text=d.get("text", ""),
        user_mentions=[
            UserMention(
                user_id=int(m["user_id"]),
                username=m["username"],
            )
            for m in d.get("user_mentions", [])
        ],
        media=[
            Media(
                media_id=int(m["media_id"]),
                media_type=m["media_type"],
            )
            for m in d.get("media", [])
        ],
        urls=[
            Url(
                shortened_url=u["shortened_url"],
                expanded_url=u["expanded_url"],
            )
            for u in d.get("urls", [])
        ],
        timestamp=int(d.get("timestamp", 0)),
        post_type=d.get("post_type", PostType.POST),
    )


# ---------------------------------------------------------------------------
# Redis serialisation
# ---------------------------------------------------------------------------

def post_to_json(post: Post) -> str:
    """Serialise Post to a compact JSON string for Redis storage."""
    return json.dumps(post_to_dict(post), separators=(",", ":"))


def json_to_post(raw: str) -> Post:
    """Deserialise a JSON string (from Redis) back to a Post struct."""
    return dict_to_post(json.loads(raw))