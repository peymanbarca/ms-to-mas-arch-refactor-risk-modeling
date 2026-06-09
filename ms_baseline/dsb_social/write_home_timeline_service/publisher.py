#!/usr/bin/env python3
"""
publisher.py — Publishes WriteHomeTimeline events to RabbitMQ.

This module is used in two ways:

1. **Imported by ComposePostService** after a post is stored, to publish
   the fan-out event:

       from publisher import HomeTimelinePublisher
       pub = HomeTimelinePublisher(rabbitmq_cfg, queue_name)
       pub.publish(req_id, post_id, user_id, timestamp, user_mentions_id, carrier)

2. **Run as a CLI** for manual testing / debugging:

       PYTHONPATH=gen-py python publisher.py \\
           --post-id 42 --user-id 1 --timestamp 1717000000000

This mirrors the C++ ComposePostService publish path which uses AMQP-CPP
to publish to the "write-home-timeline" exchange/queue.
"""

import argparse
import json
import logging
import time
import sys
import os

import pika
import pika.exceptions

from .message import encode, WriteHomeTimelineMessage

logger = logging.getLogger("write-home-timeline-publisher")

# Default RabbitMQ config (overridden by constructor args)
_DEFAULT_HOST     = "localhost"
_DEFAULT_PORT     = 5672
_DEFAULT_USERNAME = "guest"
_DEFAULT_PASSWORD = "guest"
_DEFAULT_QUEUE    = "write-home-timeline"


class HomeTimelinePublisher:
    """
    Publishes WriteHomeTimeline events to RabbitMQ.

    Parameters
    ----------
    rabbitmq_cfg : dict with host/port/username/password keys
    queue_name   : AMQP queue name (default "write-home-timeline")

    Thread safety: NOT thread-safe. Create one instance per thread,
    or wrap with a lock. ComposePostService creates one per-request context.
    """

    def __init__(
        self,
        rabbitmq_cfg: dict | None = None,
        queue_name: str = _DEFAULT_QUEUE,
    ):
        cfg = rabbitmq_cfg or {}
        self._host      = cfg.get("host",     _DEFAULT_HOST)
        self._port      = int(cfg.get("port", _DEFAULT_PORT))
        self._username  = cfg.get("username", _DEFAULT_USERNAME)
        self._password  = cfg.get("password", _DEFAULT_PASSWORD)
        self._queue     = queue_name
        self._conn      = None
        self._channel   = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open connection and channel."""
        credentials = pika.PlainCredentials(self._username, self._password)
        params = pika.ConnectionParameters(
            host=self._host,
            port=self._port,
            credentials=credentials,
            heartbeat=60,
        )
        self._conn    = pika.BlockingConnection(params)
        self._channel = self._conn.channel()
        self._channel.queue_declare(
            queue=self._queue,
            durable=True,
            arguments={"x-message-ttl": 30_000},
        )
        logger.debug("Publisher connected to RabbitMQ %s:%d", self._host, self._port)

    def close(self) -> None:
        """Close connection gracefully."""
        try:
            if self._conn and not self._conn.is_closed:
                self._conn.close()
        except Exception:
            pass
        self._conn    = None
        self._channel = None

    def __enter__(self) -> "HomeTimelinePublisher":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def publish(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        user_mentions_id: list,
        carrier: dict | None = None,
    ) -> None:
        """
        Publish a WriteHomeTimeline event.

        Parameters
        ----------
        req_id           : trace request ID
        post_id          : newly created post ID
        user_id          : author's user_id
        timestamp        : post creation timestamp in milliseconds
        user_mentions_id : list of mentioned user_ids
        carrier          : OpenTracing propagation headers (optional)
        """
        if self._channel is None or self._conn.is_closed:
            self.connect()

        msg = WriteHomeTimelineMessage(
            req_id=req_id,
            post_id=post_id,
            user_id=user_id,
            timestamp=timestamp,
            user_mentions_id=user_mentions_id or [],
            carrier=carrier or {},
        )
        body = encode(msg)

        self._channel.basic_publish(
            exchange="",
            routing_key=self._queue,
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=2,   # persistent — survives RabbitMQ restart
                content_type="application/json",
            ),
        )
        logger.debug(
            "Published req_id=%d post_id=%d user_id=%d queue=%s",
            req_id, post_id, user_id, self._queue,
        )


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Publish a WriteHomeTimeline event to RabbitMQ"
    )
    parser.add_argument("--host",       default=_DEFAULT_HOST)
    parser.add_argument("--port",       default=_DEFAULT_PORT, type=int)
    parser.add_argument("--username",   default=_DEFAULT_USERNAME)
    parser.add_argument("--password",   default=_DEFAULT_PASSWORD)
    parser.add_argument("--queue",      default=_DEFAULT_QUEUE)
    parser.add_argument("--req-id",     type=int, default=1)
    parser.add_argument("--post-id",    type=int, required=True)
    parser.add_argument("--user-id",    type=int, required=True)
    parser.add_argument("--timestamp",  type=int,
                        default=None, help="ms timestamp (default: now)")
    parser.add_argument("--mentions",   type=int, nargs="*", default=[],
                        help="mentioned user IDs")

    args = parser.parse_args()
    ts = args.timestamp or int(time.time() * 1000)

    cfg = {
        "host":     args.host,
        "port":     args.port,
        "username": args.username,
        "password": args.password,
    }

    with HomeTimelinePublisher(cfg, args.queue) as pub:
        pub.publish(
            req_id=args.req_id,
            post_id=args.post_id,
            user_id=args.user_id,
            timestamp=ts,
            user_mentions_id=args.mentions,
        )
        print(f"Published: post_id={args.post_id} user_id={args.user_id} ts={ts}")


if __name__ == "__main__":
    main()
