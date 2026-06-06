#!/usr/bin/env python3
"""
UniqueIdService — Python port of socialNetwork/src/UniqueIdService/UniqueIdService.cpp

Start command (matches docker-compose entrypoint):
    python server.py [--config /path/to/service-config.json] [--port 9090]

The original C++ binary reads service-config.json from
/social-network-microservices/config/service-config.json. We default to
./config/service-config.json and allow override via --config.
"""

import argparse
import json
import logging
import os
import sys

# ---- Thrift imports ----
from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol
from thrift.server    import TServer

# ---- Generated stubs ----
# sys.path is extended so that the gen-py directory is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))


from ms_baseline.dsb_social.gen_py.social_network import UniqueIdService

# ---- Local modules ----
from .snowflake import SnowflakeGenerator
from .handler   import UniqueIdHandler
from .tracing   import init_tracer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger("unique-id-service")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "config", "service-config.json"
)


def load_config(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def build_server(config: dict, port: int) -> TServer.TThreadedServer:
    """
    Wire up all components and return a ready-to-serve TThreadedServer.

    Mirrors the C++ main():
      - Reads machine_id from config
      - Initialises Jaeger tracer
      - Creates handler, processor, transport stack
      - Returns TThreadedServer  (C++ uses TNonblockingServer with thread pool;
        TThreadedServer is the idiomatic Python equivalent and correct for
        benchmarking at this scale)
    """
    svc_cfg    = config.get("UniqueIdService", {})
    machine_id = int(svc_cfg.get("machine_id", 0))

    logger.info("UniqueIdService starting on port %d, machine_id=%d", port, machine_id)

    # ---- Tracer ----
    tracer = init_tracer(config.get("jaeger", {}))

    # ---- Snowflake generator ----
    generator = SnowflakeGenerator(machine_id)

    # ---- Thrift handler + processor ----
    handler   = UniqueIdHandler(generator, tracer)
    processor = UniqueIdService.Processor(handler)

    # ---- Transport stack (matches C++ TFramedTransport + TBinaryProtocol) ----
    transport = TSocket.TServerSocket(port=port)
    tfactory  = TTransport.TFramedTransportFactory()
    pfactory  = TBinaryProtocol.TBinaryProtocolFactory()

    server = TServer.TThreadedServer(
        processor, transport, tfactory, pfactory,
        daemon=True,
    )
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="UniqueIdService — Python")
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH,
        help="Path to service-config.json",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override listen port (default from config or 9090)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    port = args.port
    if port is None:
        port = int(config.get("UniqueIdService", {}).get("port", 9090))

    server = build_server(config, port)

    logger.info("UniqueIdService ready")
    try:
        server.serve()
    except KeyboardInterrupt:
        logger.info("UniqueIdService shutting down")


if __name__ == "__main__":
    main()
