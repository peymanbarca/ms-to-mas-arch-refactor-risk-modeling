#!/usr/bin/env python3
"""
WriteHomeTimelineService — Python port of WriteHomeTimelineService.cpp

This service is a RabbitMQ consumer, NOT a Thrift server.
It consumes "write-home-timeline" queue messages published by
ComposePostService and forwards each to HomeTimelineService.WriteHomeTimeline.

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json]

Original C++ dependencies:
    - RabbitMQ      (write-home-timeline-rabbitmq:5672)  — message queue
    - HomeTimelineService (home-timeline-service:9099)   — downstream Thrift RPC
    - Jaeger                                             — tracing
"""

import argparse
import json
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

from ms_baseline.dsb_social.gen_py.social_network import HomeTimelineService

from .worker      import MessageWorker
from .consumer    import WriteHomeTimelineConsumer
from .thrift_pool import ThriftClientPool
from .tracing     import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("write-home-timeline-service")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_consumer(config: dict) -> WriteHomeTimelineConsumer:
    svc = config.get("WriteHomeTimelineService", {})

    # ---- HomeTimelineService client pool ----
    htc = config.get("home-timeline-service", {})
    home_pool = ThriftClientPool(
        client_class=HomeTimelineService.Client,
        host=htc.get("host", "home-timeline-service"),
        port=int(htc.get("port", 9099)),
        size=int(svc.get("num_workers", 4)) * 2,   # 2x workers for headroom
        timeout_ms=600000
    )
    logger.info(
        "HomeTimelineService pool: %s:%s", htc.get("host"), htc.get("port")
    )

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Worker (shared across all consumer threads) ----
    worker = MessageWorker(home_timeline_pool=home_pool, tracer=tracer)

    # ---- RabbitMQ consumer ----
    rabbitmq_cfg = config.get("rabbitmq", {})
    queue_name   = svc.get("rabbitmq_queue", "write-home-timeline")
    num_workers  = int(svc.get("num_workers", 4))

    return WriteHomeTimelineConsumer(
        rabbitmq_cfg=rabbitmq_cfg,
        queue_name=queue_name,
        worker=worker,
        num_workers=num_workers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="WriteHomeTimelineService — Python (RabbitMQ consumer)"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    config   = load_config(args.config)
    consumer = build_consumer(config)

    # ---- Graceful shutdown on SIGTERM / SIGINT ----
    def _shutdown(signum, frame):
        logger.info("Received signal %d — shutting down", signum)
        consumer.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    logger.info("WriteHomeTimelineService starting")
    consumer.start()
    logger.info("WriteHomeTimelineService ready — consuming messages")
    consumer.join()


if __name__ == "__main__":
    main()
