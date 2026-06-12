#!/usr/bin/env python3
"""
MediaService — AI Agent version.
Drop-in replacement for media-service/server.py.

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9091]
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
logger = logging.getLogger("media-agent")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    mc = config.get("media-mongodb", {})
    mongo_client = MongoClient(
        host=mc.get("host", "media-mongodb"),
        port=int(mc.get("port", 27017)),
        serverSelectionTimeoutMS=5000,
    )
    mongo_db  = mc.get("db",         "media")
    mongo_col = mc.get("collection", "media")
    logger.info("MongoDB: %s:%s  db=%s  col=%s",
                mc.get("host"), mc.get("port"), mongo_db, mongo_col)

    rc = config.get("media-redis", {})
    redis_client = redis.Redis(
        host=rc.get("host", "media-redis"),
        port=int(rc.get("port", 6379)),
        db=int(rc.get("db", 0)),
        password="1",
        socket_connect_timeout=5,
        decode_responses=False,
    )
    logger.info("Redis: %s:%s  db=%s", rc.get("host"), rc.get("port"), rc.get("db"))

    tracer    = init_tracer(config.get("jaeger", {}))
    handler   = MediaHandler(mongo_client, mongo_db, mongo_col, redis_client, tracer)
    processor = MediaServiceThrift.Processor(handler)

    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("MediaService Agent starting on port %d", port)
    return TServer.TThreadedServer(
        processor, transport, tfactory, pfactory, daemon=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MediaService Agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(config.get("MediaService", {}).get("port", 9091))

    server = build_server(config, port)
    logger.info("MediaService Agent ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()