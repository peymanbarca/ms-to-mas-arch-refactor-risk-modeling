#!/usr/bin/env python3
"""
MediaService — Python port of socialNetwork/src/MediaService/MediaService.cpp

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9091]

Original C++ dependencies:
    - MongoDB (media-mongodb:27017)  -> persistent store
    - Memcached (media-memcached)    -> cache   [replaced with Redis in this port]
    - Jaeger                         -> tracing
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

from ms_baseline.dsb_social.gen_py.social_network import MediaService as MediaServiceThrift

from pymongo import MongoClient
import redis

from .handler import MediaHandler
from .tracing import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("media-service")

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config", "service-config.json")


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    # ---- MongoDB ----
    mongo_cfg = config.get("media-mongodb", {})
    mongo_host = mongo_cfg.get("host", "localhost")
    mongo_port = int(mongo_cfg.get("port", 27017))
    mongo_db   = mongo_cfg.get("db",   "dsb_social")
    mongo_col  = mongo_cfg.get("collection", "media")

    logger.info("Connecting to MongoDB at %s:%d db=%s col=%s",
                mongo_host, mongo_port, mongo_db, mongo_col)
    mongo_client = MongoClient(
        host=mongo_host,
        port=mongo_port,
        serverSelectionTimeoutMS=5000,
    )

    # ---- Redis (replaces Memcached) ----
    redis_cfg  = config.get("media-redis", {})
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
    handler   = MediaHandler(mongo_client, mongo_db, mongo_col, redis_client, tracer)
    processor = MediaServiceThrift.Processor(handler)

    # ---- Thrift transport stack ----
    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("MediaService starting on port %d", port)
    return TServer.TThreadedServer(processor, transport, tfactory, pfactory, daemon=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="MediaService — Python")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(config.get("MediaService", {}).get("port", 9091))

    server = build_server(config, port)
    logger.info("MediaService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("MediaService shutting down")


if __name__ == "__main__":
    main()