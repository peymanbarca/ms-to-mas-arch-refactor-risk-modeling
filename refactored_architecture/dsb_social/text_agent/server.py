#!/usr/bin/env python3
"""
TextService — AI Agent version.
Drop-in replacement for text-service/server.py.

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9095]
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

from .handler     import TextHandler
from .thrift_pool import ThriftClientPool
from .tracing     import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("text-agent")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    svc = config.get("TextService", {})
    pool_size = int(svc.get("num_workers", 8))

    url_cfg  = config.get("url-shorten-service", {})
    url_pool = ThriftClientPool(
        client_class=UrlShortenService.Client,
        host=url_cfg.get("host", "url-shorten-service"),
        port=int(url_cfg.get("port", 9092)),
        size=pool_size,
    )
    logger.info("UrlShortenService pool: %s:%s",
                url_cfg.get("host"), url_cfg.get("port"))

    mention_cfg  = config.get("user-mention-service", {})
    mention_pool = ThriftClientPool(
        client_class=UserMentionService.Client,
        host=mention_cfg.get("host", "user-mention-service"),
        port=int(mention_cfg.get("port", 9093)),
        size=pool_size,
    )
    logger.info("UserMentionService pool: %s:%s",
                mention_cfg.get("host"), mention_cfg.get("port"))

    tracer    = init_tracer(config.get("jaeger", {}))
    handler   = TextHandler(url_pool, mention_pool, tracer)
    processor = TextServiceThrift.Processor(handler)

    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("TextService Agent starting on port %d", port)
    return TServer.TThreadedServer(
        processor, transport, tfactory, pfactory, daemon=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="TextService Agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(config.get("TextService", {}).get("port", 9095))

    server = build_server(config, port)
    logger.info("TextService Agent ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()