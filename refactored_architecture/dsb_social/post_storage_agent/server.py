#!/usr/bin/env python3
"""
PostStorageService — Python port of socialNetwork/src/PostStorageService/PostStorageService.cpp

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9096]

Original C++ dependencies:
    - MongoDB (post-storage-mongodb:27017)  -> persistent store
    - Memcached (post-storage-memcached)    -> cache   [replaced with Redis in this port]
    - Jaeger                                -> tracing
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

from ms_baseline.dsb_social.gen_py.social_network import PostStorageService as PostStorageServiceThrift

from pymongo import MongoClient
import redis

from .handler import PostStorageHandler
from .tracing import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("post-storage-service")

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config", "service_config.json")


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    # ---- MongoDB ----
    mongo_cfg = config.get("post-storage-mongodb", {})
    mongo_host = mongo_cfg.get("host", "localhost")
    mongo_port = int(mongo_cfg.get("port", 27017))
    mongo_db   = mongo_cfg.get("db",   "post")
    mongo_col  = mongo_cfg.get("collection", "post")

    logger.info("Connecting to MongoDB at %s:%d db=%s col=%s",
                mongo_host, mongo_port, mongo_db, mongo_col)
    mongo_client = MongoClient(
        host=mongo_host,
        port=mongo_port,
        serverSelectionTimeoutMS=5000,
    )

    # ---- Redis (replaces Memcached) ----
    redis_cfg  = config.get("post-storage-redis", {})
    redis_host = redis_cfg.get("host", "localhost")
    redis_port = int(redis_cfg.get("port", 6379))
    redis_db   = int(redis_cfg.get("db",   0))

    logger.info("Connecting to Redis at %s:%d db=%d", redis_host, redis_port, redis_db)
    redis_client = redis.Redis(
        host=redis_host,
        port=redis_port,
        db=redis_db,
        password='1',
        socket_connect_timeout=5,
        decode_responses=False,
    )

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Handler + Processor ----
    num_workers = config.get("PostStorageService", {}).get("num_workers", 8)
    handler   = PostStorageHandler(mongo_client, mongo_db, mongo_col, redis_client, tracer, num_workers)
    processor = PostStorageServiceThrift.Processor(handler)

    # ---- Thrift transport stack ----
    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("PostStorageService starting on port %d", port)
    return TServer.TThreadedServer(processor, transport, tfactory, pfactory, daemon=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="PostStorageService — Python")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(config.get("PostStorageService", {}).get("port", 9096))

    server = build_server(config, port)
    logger.info("PostStorageService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("PostStorageService shutting down")


if __name__ == "__main__":
    main()
