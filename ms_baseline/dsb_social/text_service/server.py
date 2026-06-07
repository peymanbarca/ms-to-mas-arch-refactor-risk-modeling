#!/usr/bin/env python3
"""
TextService — Python port of TextService.cpp

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9095]

Original C++ dependencies (no databases — pure computation + downstream RPCs):
    - UrlShortenService  (url-shorten-service:9092)
    - UserMentionService (user-mention-service:9093)
    - Jaeger
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
    TextService as TextServiceThrift,
    UrlShortenService,
    UserMentionService,
)

from .handler      import TextHandler
from .thrift_pool  import ThriftClientPool
from .tracing      import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("text-service")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    # ---- UrlShortenService client pool ----
    url_cfg  = config.get("url-shorten-service", {})
    url_host = url_cfg.get("host", "url-shorten-service")
    url_port = int(url_cfg.get("port", 9092))
    logger.info("UrlShortenService pool: %s:%d", url_host, url_port)

    url_pool = ThriftClientPool(
        client_class=UrlShortenService.Client,
        host=url_host,
        port=url_port,
        size=config.get("TextService", {}).get("num_workers", 8),
    )

    # ---- UserMentionService client pool ----
    mention_cfg  = config.get("user-mention-service", {})
    mention_host = mention_cfg.get("host", "user-mention-service")
    mention_port = int(mention_cfg.get("port", 9093))
    logger.info("UserMentionService pool: %s:%d", mention_host, mention_port)

    mention_pool = ThriftClientPool(
        client_class=UserMentionService.Client,
        host=mention_host,
        port=mention_port,
        size=config.get("TextService", {}).get("num_workers", 8),
    )

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Handler + Processor ----
    handler   = TextHandler(url_pool, mention_pool, tracer)
    processor = TextServiceThrift.Processor(handler)

    # ---- Transport stack ----
    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("TextService starting on port %d", port)
    return TServer.TThreadedServer(
        processor, transport, tfactory, pfactory, daemon=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="TextService — Python")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(config.get("TextService", {}).get("port", 9095))

    server = build_server(config, port)
    logger.info("TextService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("TextService shutting down")


if __name__ == "__main__":
    main()
