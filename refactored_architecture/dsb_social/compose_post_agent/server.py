#!/usr/bin/env python3
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
    ComposePostService as ComposePostServiceThrift,
    UniqueIdService,
    MediaService,
    TextService,
    UserService,
    PostStorageService,
    UserTimelineService,
)

from .agent_handler_v2 import ComposePostHandler
from .thrift_pool import ThriftClientPool
from .tracing import init_tracer

import pika

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("compose-post-service")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


class _RabbitPublisher:
    def __init__(self, cfg: dict, queue_name: str):
        self._host = cfg.get("host", "write-home-timeline-rabbitmq")
        self._port = int(cfg.get("port", 5672))
        self._username = cfg.get("username", "guest")
        self._password = cfg.get("password", "guest")
        self._queue = queue_name

    def publish(
        self,
        req_id: int,
        post_id: int,
        user_id: int,
        timestamp: int,
        user_mentions_id: list,
        carrier: dict,
    ) -> None:
        payload = json.dumps(
            {
                "req_id": req_id,
                "post_id": post_id,
                "user_id": user_id,
                "timestamp": timestamp,
                "user_mentions_id": user_mentions_id,
                "carrier": carrier,
            },
            separators=(",", ":"),
        ).encode()

        creds = pika.PlainCredentials(self._username, self._password)
        params = pika.ConnectionParameters(
            host=self._host,
            port=self._port,
            credentials=creds,
            heartbeat=60,
        )
        conn = pika.BlockingConnection(params)
        channel = conn.channel()
        channel.queue_declare(
            queue=self._queue,
            durable=True,
            arguments={"x-message-ttl": 30_000},
        )
        channel.basic_publish(
            exchange="",
            routing_key=self._queue,
            body=payload,
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
            ),
        )
        conn.close()
        logger.debug("Published post_id=%d to %s", post_id, self._queue)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _pool(client_class, cfg_key: str, config: dict, size: int) -> ThriftClientPool:
    sec = config.get(cfg_key, {})
    host = sec.get("host", cfg_key)
    port = int(sec.get("port", 9090))
    logger.info("Pool %-28s %s:%d  size=%d", cfg_key, host, port, size)
    return ThriftClientPool(client_class, host, port, size=size)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    svc = config.get("ComposePostService", {})
    num_workers = int(svc.get("num_workers", 8))
    pool_size = num_workers * 2

    unique_id_pool = _pool(UniqueIdService.Client, "unique-id-service", config, pool_size)
    media_pool = _pool(MediaService.Client, "media-service", config, pool_size)
    text_pool = _pool(TextService.Client, "text-service", config, pool_size)
    user_pool = _pool(UserService.Client, "user-service", config, pool_size)
    post_pool = _pool(PostStorageService.Client, "post-storage-service", config, pool_size)
    timeline_pool = _pool(UserTimelineService.Client, "user-timeline-service", config, pool_size)

    mq_cfg = config.get("rabbitmq", {})
    queue_name = config.get("write-home-timeline-queue", "write-home-timeline")
    publisher = _RabbitPublisher(mq_cfg, queue_name)

    tracer = init_tracer(config.get("jaeger", {}))

    ollama_cfg = config.get("ollama", {})
    model_name = ollama_cfg.get("model", "llama3.2:3b")
    base_url = ollama_cfg.get("base_url", "http://localhost:11434")
    temperature = float(ollama_cfg.get("temperature", 0.0))

    handler = ComposePostHandler(
        unique_id_pool=unique_id_pool,
        text_pool=text_pool,
        user_pool=user_pool,
        media_pool=media_pool,
        post_storage_pool=post_pool,
        user_timeline_pool=timeline_pool,
        publisher=publisher,
        tracer=tracer,
        ollama_model=model_name,
        ollama_base_url=base_url,
        temperature=temperature,
        num_workers=num_workers,
    )
    processor = ComposePostServiceThrift.Processor(handler)

    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory = TTransport.TFramedTransportFactory()
    pfactory = TBinaryProtocol.TBinaryProtocolFactory()

    logger.info("ComposePostService starting on port %d", port)
    return TServer.TThreadedServer(
        processor,
        transport,
        tfactory,
        pfactory,
        daemon=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="ComposePostService — AI agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or int(config.get("ComposePostService", {}).get("port", 9100))

    server = build_server(config, port)
    logger.info("ComposePostService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("ComposePostService shutting down")


if __name__ == "__main__":
    main()