#!/usr/bin/env python3
"""
HomeTimelineService — Python port of socialNetwork/src/HomeTimelineService/HomeTimelineService.cpp

Usage:
    PYTHONPATH=gen-py python server.py [--config service-config.json] [--port 9099]

Original C++ dependencies:
    - Redis (home-timeline-redis:6379)       -> timeline storage
    - PostStorageService (9096)              -> post hydration
    - SocialGraphService (9097)              -> follower list
    - Jaeger                                 -> tracing
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

from ms_baseline.dsb_social.gen_py.social_network import HomeTimelineService as HomeTimelineServiceThrift
from ms_baseline.dsb_social.gen_py.social_network import PostStorageService, SocialGraphService

import redis

from .handler import HomeTimelineHandler
from .thrift_pool import ThriftClientPool
from .tracing import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("home-timeline-agent")

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config", "service-config.json")


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    # ---- Redis ----
    redis_cfg  = config.get("home-timeline-redis", {})
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

    # ---- PostStorageService client pool ----
    post_storage_cfg = config.get("post-storage-service", {})
    post_host = post_storage_cfg.get("host", "localhost")
    post_port = int(post_storage_cfg.get("port", 9096))

    logger.info("Setting up PostStorageService pool at %s:%d", post_host, post_port)
    post_storage_pool = ThriftClientPool(
        PostStorageService.Client,
        host=post_host,
        port=post_port,
        size=16,
        timeout_ms=600000,
    )

    # ---- SocialGraphService client pool ----
    social_graph_cfg = config.get("social-graph-service", {})
    graph_host = social_graph_cfg.get("host", "localhost")
    graph_port = int(social_graph_cfg.get("port", 9097))

    logger.info("Setting up SocialGraphService pool at %s:%d", graph_host, graph_port)
    social_graph_pool = ThriftClientPool(
        SocialGraphService.Client,
        host=graph_host,
        port=graph_port,
        size=16,
        timeout_ms=600000,
    )

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Handler + Processor ----
    num_workers = config.get("HomeTimelineService", {}).get("num_workers", 16)
    handler   = HomeTimelineHandler(redis_client, post_storage_pool, social_graph_pool, tracer, num_workers)
    processor = HomeTimelineServiceThrift.Processor(handler)

    # ---- Thrift transport stack ----
    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("HomeTimelineService starting on port %d", port)
    return TServer.TThreadedServer(processor, transport, tfactory, pfactory, daemon=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="HomeTimelineService — Python")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(config.get("HomeTimelineService", {}).get("port", 9099))

    server = build_server(config, port)
    logger.info("HomeTimelineService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("HomeTimelineService shutting down")


if __name__ == "__main__":
    main()
