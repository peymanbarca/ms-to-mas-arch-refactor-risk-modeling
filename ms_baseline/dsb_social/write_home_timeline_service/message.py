"""
message.py — WriteHomeTimeline event message schema.

The C++ ComposePostService publishes a JSON-encoded event to the
"write-home-timeline" RabbitMQ queue after a post is stored. The
WriteHomeTimelineService consumes these messages and forwards them to
HomeTimelineService.WriteHomeTimeline.

Message payload (JSON):
{
    "req_id":           i64,
    "post_id":          i64,
    "user_id":          i64,
    "timestamp":        i64,    -- millisecond Unix timestamp
    "user_mentions_id": [i64, ...],
    "carrier":          {str: str}  -- OpenTracing propagation headers
}

This schema exactly matches the C++ nlohmann::json serialisation in
ComposePostService/ComposePostHandler.h.
"""

import json
from dataclasses import dataclass, field


@dataclass
class WriteHomeTimelineMessage:
    req_id:           int
    post_id:          int
    user_id:          int
    timestamp:        int
    user_mentions_id: list
    carrier:          dict = field(default_factory=dict)


def encode(msg: WriteHomeTimelineMessage) -> bytes:
    """Serialise to JSON bytes for publishing."""
    return json.dumps({
        "req_id":           msg.req_id,
        "post_id":          msg.post_id,
        "user_id":          msg.user_id,
        "timestamp":        msg.timestamp,
        "user_mentions_id": msg.user_mentions_id,
        "carrier":          msg.carrier,
    }, separators=(",", ":")).encode("utf-8")


def decode(raw: bytes) -> WriteHomeTimelineMessage:
    """Deserialise from raw bytes consumed from RabbitMQ."""
    d = json.loads(raw.decode("utf-8"))
    return WriteHomeTimelineMessage(
        req_id=int(d["req_id"]),
        post_id=int(d["post_id"]),
        user_id=int(d["user_id"]),
        timestamp=int(d["timestamp"]),
        user_mentions_id=[int(uid) for uid in d.get("user_mentions_id", [])],
        carrier=d.get("carrier", {}),
    )
