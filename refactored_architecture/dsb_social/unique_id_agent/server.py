#!/usr/bin/env python3
"""
UniqueIdService — AI Agent version.

Drop-in replacement for the original unique-id-service/server.py.
Identical startup, config file, port, and Thrift transport stack.
The only change is that handler.py now drives the LangGraph agent.

Usage:
    PYTHONPATH=gen-py python server.py [--config config/service-config.json] [--port 9090]
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

from ms_baseline.dsb_social.gen_py.social_network import UniqueIdService

from .snowflake_agent import AgentSnowflakeGenerator
from .handler         import UniqueIdHandler
from .tracing         import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("unique-id-agent")

DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "config", "service-config.json")


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    svc_cfg    = config.get("UniqueIdService", {})
    machine_id = int(svc_cfg.get("machine_id", 0))

    logger.info("UniqueIdAgent starting port=%d machine_id=%d", port, machine_id)

    tracer    = init_tracer(config.get("jaeger", {}))
    generator = AgentSnowflakeGenerator(machine_id)
    handler   = UniqueIdHandler(generator, tracer)
    processor = UniqueIdService.Processor(handler)

    transport = TSocket.TServerSocket(host="0.0.0.0", port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    return TServer.TThreadedServer(
        processor, transport, tfactory, pfactory, daemon=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="UniqueIdService Agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port",   type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    port   = args.port or int(config.get("UniqueIdService", {}).get("port", 9090))

    server = build_server(config, port)
    logger.info("UniqueIdService Agent ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
