#!/usr/bin/env python3
"""
UserMentionService — Python port of UserMentionService.cpp

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9093]

Original C++ dependencies:
    - MongoDB   (user-mongodb:27017)     — shared "user" collection (read-only)
    - Memcached (user-mention-memcached) — username→user_id cache  [replaced with Redis]
    - Jaeger                             — tracing

Important: This service shares the same MongoDB "user" collection with
UserService. It never writes to it — only reads. The MongoDB host in
service-config.json must point to the same instance as UserService uses.
"""

import argparse
import json
import logging
import os
import sys

from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol
from thrift.server    import TServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

from ms_baseline.dsb_social.gen_py.social_network import UserMentionService as UserMentionServiceThrift

from pymongo import MongoClient
import redis

from .handler import UserMentionHandler
from .tracing import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("user-mention-service")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    # ---- MongoDB ----
    # Shares the "user" database/collection with UserService (read-only).
    mc = config.get("user-mention-mongodb", {})
    mongo_host = mc.get("host", "user-mongodb")
    mongo_port = int(mc.get("port", 27017))
    mongo_db   = mc.get("db",         "user")
    mongo_col  = mc.get("collection", "user")

    logger.info(
        "MongoDB: %s:%d  db=%s  col=%s  (shared with UserService, read-only)",
        mongo_host, mongo_port, mongo_db, mongo_col,
    )
    mongo_client = MongoClient(
        host=mongo_host,
        port=mongo_port,
        serverSelectionTimeoutMS=5000,
    )

    # ---- Redis (replaces Memcached) ----
    rc = config.get("user-mention-redis", {})
    redis_host = rc.get("host", "user-mention-redis")
    redis_port = int(rc.get("port", 6379))
    redis_db   = int(rc.get("db",   0))

    logger.info("Redis: %s:%d  db=%d", redis_host, redis_port, redis_db)
    redis_client = redis.Redis(
        host=redis_host,
        port=redis_port,
        db=redis_db,
        password="1",
        socket_connect_timeout=5,
        decode_responses=False,
    )

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Handler + Processor ----
    handler   = UserMentionHandler(
        mongo_client, mongo_db, mongo_col, redis_client, tracer
    )
    processor = UserMentionServiceThrift.Processor(handler)

    # ---- Thrift transport stack ----
    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("UserMentionService starting on port %d", port)
    return TServer.TThreadedServer(
        processor, transport, tfactory, pfactory, daemon=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="UserMentionService — Python")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(
        config.get("UserMentionService", {}).get("port", 9093)
    )

    server = build_server(config, port)
    logger.info("UserMentionService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("UserMentionService shutting down")


if __name__ == "__main__":
    main()
