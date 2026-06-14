#!/usr/bin/env python3
"""
SocialGraphService — Python port of SocialGraphService.cpp

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9097]

Original C++ dependencies:
    - MongoDB   (social-graph-mongodb:27017)  — persistent graph store
    - Redis     (social-graph-redis:6379)     — sorted-set cache (same in original)
    - UserService (user-service:9094)         — username resolution for *WithUsername
    - Jaeger                                  — tracing

Note: Unlike most other services, the C++ SocialGraphService uses Redis
(not Memcached) directly for its cache. This port maintains that behaviour.
"""

import argparse
import json
import logging
import os
import sys

from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol
from thrift.server import TServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

from ms_baseline.dsb_social.gen_py.social_network import (
    SocialGraphService as SocialGraphServiceThrift,
    UserService,
)

from pymongo import MongoClient
import redis

from .handler import SocialGraphHandler
from .thrift_pool import ThriftClientPool
from .tracing import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("social-graph-service")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    svc = config.get("SocialGraphService", {})
    num_workers = int(svc.get("num_workers", 8))

    # ---- MongoDB ----
    mc = config.get("social-graph-mongodb", {})
    mongo_client = MongoClient(
        host=mc.get("host", "social-graph-mongodb"),
        port=int(mc.get("port", 27017)),
        serverSelectionTimeoutMS=5000,
    )
    mongo_db = mc.get("db", "social-graph")
    mongo_col = mc.get("collection", "social-graph")
    logger.info(
        "MongoDB: %s:%s  db=%s  col=%s",
        mc.get("host"), mc.get("port"), mongo_db, mongo_col
    )

    # ---- Redis ----
    rc = config.get("social-graph-redis", {})
    redis_client = redis.Redis(
        host=rc.get("host", "social-graph-redis"),
        port=int(rc.get("port", 6379)),
        db=int(rc.get("db", 0)),
        password="1",
        socket_connect_timeout=5,
        decode_responses=False,
    )
    logger.info(
        "Redis: %s:%s  db=%s",
        rc.get("host"), rc.get("port"), rc.get("db")
    )

    # ---- UserService client pool ----
    uc = config.get("user-service", {})
    user_pool = ThriftClientPool(
        client_class=UserService.Client,
        host=uc.get("host", "user-service"),
        port=int(uc.get("port", 9094)),
        size=num_workers,
    )
    logger.info("UserService pool: %s:%s", uc.get("host"), uc.get("port"))

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Handler + Processor ----
    handler = SocialGraphHandler(
        mongo_client,
        mongo_db,
        mongo_col,
        redis_client,
        user_pool,
        tracer,
        num_workers=num_workers,
    )
    processor = SocialGraphServiceThrift.Processor(handler)

    # ---- Transport stack ----
    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory = TTransport.TFramedTransportFactory()
    pfactory = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("SocialGraphService starting on port %d", port)

    # Keep the standard threaded server; remove daemon kwarg for compatibility.
    return TServer.TThreadedServer(
        processor,
        transport,
        tfactory,
        pfactory,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="SocialGraphService — Python")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or int(
        config.get("SocialGraphService", {}).get("port", 9097)
    )

    server = build_server(config, port)
    logger.info("SocialGraphService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("SocialGraphService shutting down")


if __name__ == "__main__":
    main()