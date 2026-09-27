#!/usr/bin/env python3
"""
client.py — Python client for ComposePostService with Strangler Pattern support

Usage examples
--------------

# Compose a plain text post with Strangler V2 rate set via environment variable
STRANGLER_V2_RATE=30 python client.py compose --username alice --user-id 1 --text "Hello world!"

# Custom V1 and V2 host/port configuration programmatically or via CLI
python client.py --host-v1 127.0.0.1 --port-v1 9100 --host-v2 127.0.0.1 --port-v2 9200 \
    --strangler-v2-rate 50 compose --username alice --user-id 1 --text "Hello!"
"""

import argparse
import sys
import os
import time
import random
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

from thrift.transport import TSocket, TTransport
from thrift.transport.TTransport import TTransportException
from thrift.protocol  import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import ComposePostService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import PostType, ServiceException

logger = logging.getLogger("compose-post-client")

_POST_TYPE_MAP = {
    "POST":   PostType.POST,
    "REPOST": PostType.REPOST,
    "REPLY":  PostType.REPLY,
    "DM":     PostType.DM,
}


class ComposePostClient:
    """
    Thrift client for ComposePostService supporting the Strangler Pattern.

    Parameters
    ----------
    host_v1, port_v1     : V1 service address
    host_v2, port_v2     : V2 service address
    strangler_v2_rate    : Percentage (0-100) of traffic routed to V2
    timeout_ms           : socket timeout ms (default 100000)
    max_retries          : connection attempts (default 3)
    retry_delay          : seconds between retries (default 0.5)
    """

    def __init__(
        self,
        host_v1: str = "127.0.0.1",
        port_v1: int = 9100, # service
        host_v2: str = "127.0.0.1",
        port_v2: int = 10100, # agent
        strangler_v2_rate: int = 0,
        timeout_ms: int = 100000,
        max_retries: int = 3,
        retry_delay: float = 0.5,
        req_id: int | None = None,
    ):
        self._host_v1          = host_v1
        self._port_v1          = port_v1
        self._host_v2          = host_v2
        self._port_v2          = port_v2
        self._strangler_v2_rate = strangler_v2_rate
        self._timeout_ms       = timeout_ms
        self._max_retries      = max_retries
        self._retry_delay      = retry_delay
        self._req_id           = req_id if req_id is not None else random.randint(1, 2**31)
        self._transport        = None
        self._client           = None
        self.active_version    = "V1"

    # ------------------------------------------------------------------
    # Strangler Routing Logic
    # ------------------------------------------------------------------

    def _resolve_target(self) -> tuple[str, int, str]:
        """
        Determines whether to target V1 or V2 based on the strangler V2 rate percentage.
        Returns (host, port, version_tag).
        """
        if random.randint(1, 100) <= self._strangler_v2_rate:
            return self._host_v2, self._port_v2, "V2"
        return self._host_v1, self._port_v1, "V1"

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        target_host, target_port, self.active_version = self._resolve_target()
        last_exc = None
        
        for attempt in range(1, self._max_retries + 1):
            try:
                sock = TSocket.TSocket(target_host, target_port)
                sock.setTimeout(self._timeout_ms)
                transport = TTransport.TFramedTransport(sock)
                protocol  = TBinaryProtocol.TBinaryProtocol(transport)
                transport.open()
                self._transport = transport
                self._client    = ComposePostService.Client(protocol)
                logger.debug("Connected to ComposePostService [%s] at %s:%d",
                             self.active_version, target_host, target_port)
                return
            except TTransportException as exc:
                last_exc = exc
                logger.warning("Connection attempt %d/%d to [%s] (%s:%d) failed: %s",
                               attempt, self._max_retries, self.active_version, target_host, target_port, exc)
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
                    
        raise ConnectionError(
            f"Could not connect to ComposePostService [{self.active_version}] at {target_host}:{target_port} "
            f"after {self._max_retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        if self._transport and self._transport.isOpen():
            self._transport.close()
        self._transport = None
        self._client    = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.isOpen()

    def __enter__(self) -> "ComposePostClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Service call
    # ------------------------------------------------------------------

    def compose_post(
        self,
        username: str,
        user_id: int,
        text: str,
        media_ids: list | None = None,
        media_types: list | None = None,
        post_type=PostType.POST,
        carrier: dict | None = None,
    ) -> None:
        self._ensure_connected()
        req_id = self._next_req_id()
        logger.debug(
            "ComposePost [%s] req_id=%d username=%s user_id=%d",
            self.active_version, req_id, username, user_id,
        )
        try:
            self._client.ComposePost(
                req_id,
                username,
                user_id,
                text,
                media_ids   or [],
                media_types or [],
                post_type,
                carrier     or {},
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(f"Transport error during ComposePost [{self.active_version}]: {exc}") from exc

    ComposePost = compose_post

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected. Use 'with ComposePostClient(...) as c:'"
            )

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def _ok(username: str, text: str, version: str) -> None:
    print(f"\n  Post composed successfully [{version}].")
    print(f"  author : @{username}")
    print(f"  text   : {text[:80]!r}")
    print()


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_repl(client: ComposePostClient) -> None:
    print(f"ComposePostService REPL [Strangler Rate: {client._strangler_v2_rate}% V2]")
    print("Commands:")
    print("  post <username> <user_id> <text>")
    print("  quit\n")

    while True:
        try:
            line = input("compose-post> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not line:
            continue
        parts = line.split(None, 3)
        cmd   = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            print("Bye!")
            break
        elif cmd == "post":
            if len(parts) < 4:
                print("Usage: post <username> <user_id> <text>")
                continue
            _, username, user_id_str, text = parts
            try:
                # Re-connect to handle per-request/per-session strangler routing flows if desired
                # Or invoke using current bound context client
                client.compose_post(username, int(user_id_str), text)
                _ok(username, text, client.active_version)
            except (ServiceException, ConnectionError) as exc:
                print(f"[ERROR] {exc}")
        else:
            print(f"Unknown command: {cmd!r}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="ComposePostService Python client with Strangler Pattern support",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Strangler Pattern Configuration Arguments
    parser.add_argument("--host-v1",           default=os.environ.get("HOST_V1", "127.0.0.1"))
    parser.add_argument("--port-v1",           default=int(os.environ.get("PORT_V1", 9100)), type=int)
    parser.add_argument("--host-v2",           default=os.environ.get("HOST_V2", "127.0.0.1"))
    parser.add_argument("--port-v2",           default=int(os.environ.get("PORT_V2", 9200)), type=int)
    parser.add_argument("--strangler-v2-rate", default=int(os.environ.get("STRANGLER_V2_RATE", "90")), type=int,
                        help="Percentage (0-100) of traffic routed to V2")
    
    parser.add_argument("--timeout",           default=200000, type=int)
    parser.add_argument("--retries",           default=3,     type=int)
    parser.add_argument("-v", "--verbose",     action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("compose", help="Create a post")
    p.add_argument("--username",    required=True)
    p.add_argument("--user-id",     type=int, required=True)
    p.add_argument("--text",        required=True)
    p.add_argument("--media-ids",   type=int, nargs="*", default=[])
    p.add_argument("--media-types", nargs="*", default=[])
    p.add_argument("--post-type",   default="POST",
                   choices=["POST", "REPOST", "REPLY", "DM"])

    sub.add_parser("repl", help="Interactive REPL")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        with ComposePostClient(
            host_v1=args.host_v1,
            port_v1=args.port_v1,
            host_v2=args.host_v2,
            port_v2=args.port_v2,
            strangler_v2_rate=args.strangler_v2_rate,
            timeout_ms=args.timeout,
            max_retries=args.retries,
        ) as c:
            if args.command == "compose":
                c.compose_post(
                    username=args.username,
                    user_id=args.user_id,
                    text=args.text,
                    media_ids=args.media_ids,
                    media_types=args.media_types,
                    post_type=_POST_TYPE_MAP[args.post_type],
                )
                _ok(args.username, args.text, c.active_version)
            elif args.command == "repl":
                run_repl(c)

    except ConnectionError as exc:
        print(f"[CONNECTION ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except ServiceException as exc:
        print(f"[SERVICE ERROR] {exc.message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()