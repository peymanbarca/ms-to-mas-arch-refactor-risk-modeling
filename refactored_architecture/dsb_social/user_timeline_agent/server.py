#!/usr/bin/env python3
"""
UserTimelineService — Python port of UserTimelineService.cpp

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9098]

Original C++ dependencies:
    - MongoDB          (user-timeline-mongodb:27017) — persistent timeline store
    - Redis            (user-timeline-redis:6379)    — sorted-set timeline cache
    - PostStorageService (post-storage-service:9096) — post hydration on read
    - Jaeger                                         — tracing
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

from ms_baseline.dsb_social.gen_py.social_network import (
    UserTimelineService as UserTimelineServiceThrift,
    PostStorageService,
)

from pymongo import MongoClient
import redis

from .handler     import UserTimelineHandler
from .thrift_pool import ThriftClientPool
from .tracing     import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("user-timeline-service")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    svc = config.get("UserTimelineService", {})

    # ---- MongoDB ----
    mc = config.get("user-timeline-mongodb", {})
    mongo_client = MongoClient(
        host=mc.get("host", "user-timeline-mongodb"),
        port=int(mc.get("port", 27017)),
        serverSelectionTimeoutMS=5000,
    )
    mongo_db  = mc.get("db",         "user-timeline")
    mongo_col = mc.get("collection", "user-timeline")
    logger.info("MongoDB: %s:%s  db=%s  col=%s",
                mc.get("host"), mc.get("port"), mongo_db, mongo_col)

    # ---- Redis ----
    rc = config.get("user-timeline-redis", {})
    redis_client = redis.Redis(
        host=rc.get("host", "user-timeline-redis"),
        port=int(rc.get("port", 6379)),
        db=int(rc.get("db", 0)),
        password='1',
        socket_connect_timeout=5,
        decode_responses=False,
    )
    logger.info("Redis: %s:%s  db=%s", rc.get("host"), rc.get("port"), rc.get("db"))

    # ---- PostStorageService client pool ----
    pc = config.get("post-storage-service", {})
    post_pool = ThriftClientPool(
        client_class=PostStorageService.Client,
        host=pc.get("host", "post-storage-service"),
        port=int(pc.get("port", 9096)),
        size=int(svc.get("num_workers", 8)),
    )
    logger.info("PostStorageService pool: %s:%s",
                pc.get("host"), pc.get("port"))

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Handler + Processor ----
    handler   = UserTimelineHandler(
        mongo_client, mongo_db, mongo_col,
        redis_client, post_pool, tracer,
        num_workers=int(svc.get("num_workers", 8)),
    )
    processor = UserTimelineServiceThrift.Processor(handler)

    # ---- Transport stack ----
    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("UserTimelineService starting on port %d", port)
    return TServer.TThreadedServer(
        processor, transport, tfactory, pfactory, daemon=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="UserTimelineService — Python")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(
        config.get("UserTimelineService", {}).get("port", 9098)
    )

    server = build_server(config, port)
    logger.info("UserTimelineService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("UserTimelineService shutting down")


if __name__ == "__main__":
    main()
