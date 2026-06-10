#!/usr/bin/env python3
"""
client.py — Python client for ComposePostService

Usage examples
--------------

# Compose a plain text post
python client.py compose --username alice --user-id 1 --text "Hello world!"

# Compose with media
python client.py compose --username alice --user-id 1 \\
    --text "Check this out!" --media-ids 100 101 --media-types photo photo

# Compose a repost
python client.py compose --username alice --user-id 1 \\
    --text "RT @bob great post" --post-type REPOST

# Custom host/port
python client.py --host 127.0.0.1 --port 9100 compose \\
    --username alice --user-id 1 --text "Hello!"

# Interactive REPL
python client.py repl

Programmatic usage
------------------
from client import ComposePostClient

with ComposePostClient("127.0.0.1", 9100) as c:
    c.compose_post(
        username="alice",
        user_id=1,
        text="Hello @bob, see https://example.com",
        media_ids=[],
        media_types=[],
        post_type=PostType.POST,
    )
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
    Thrift client for ComposePostService.

    Context-manager usage (recommended):

        with ComposePostClient("127.0.0.1", 9100) as c:
            c.compose_post("alice", 1, "Hello!", [], [], PostType.POST)

    Parameters
    ----------
    host, port      : service address
    timeout_ms      : socket timeout ms    (default 10000 — ComposePost is slow)
    max_retries     : connection attempts  (default 3)
    retry_delay     : seconds between retries (default 0.5)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9100,
        timeout_ms: int = 10000,   # longer default — orchestrates many services
        max_retries: int = 3,
        retry_delay: float = 0.5,
        req_id: int | None = None,
    ):
        self._host        = host
        self._port        = port
        self._timeout_ms  = timeout_ms
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._req_id      = req_id if req_id is not None else random.randint(1, 2**31)
        self._transport   = None
        self._client      = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                sock = TSocket.TSocket(self._host, self._port)
                sock.setTimeout(self._timeout_ms)
                transport = TTransport.TFramedTransport(sock)
                protocol  = TBinaryProtocol.TBinaryProtocol(transport)
                transport.open()
                self._transport = transport
                self._client    = ComposePostService.Client(protocol)
                logger.debug("Connected to ComposePostService at %s:%d",
                             self._host, self._port)
                return
            except TTransportException as exc:
                last_exc = exc
                logger.warning("Connection attempt %d/%d failed: %s",
                               attempt, self._max_retries, exc)
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
        raise ConnectionError(
            f"Could not connect to ComposePostService at {self._host}:{self._port} "
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
        """
        Orchestrate a full post creation across all downstream services.

        Parameters
        ----------
        username    : author's username (from JWT)
        user_id     : author's user_id  (from JWT)
        text        : raw post text (may contain URLs and @mentions)
        media_ids   : list of pre-assigned media IDs (optional)
        media_types : list of media type strings (same length as media_ids)
        post_type   : PostType enum value (default POST)
        carrier     : OpenTracing propagation headers (optional)
        """
        self._ensure_connected()
        req_id = self._next_req_id()
        logger.debug(
            "ComposePost req_id=%d username=%s user_id=%d",
            req_id, username, user_id,
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
            raise ConnectionError(f"Transport error during ComposePost: {exc}") from exc

    # Alias matching Thrift method name
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

def _ok(username: str, text: str) -> None:
    print(f"\n  Post composed successfully.")
    print(f"  author : @{username}")
    print(f"  text   : {text[:80]!r}")
    print()


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_repl(client: ComposePostClient) -> None:
    print("ComposePostService REPL")
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
                client.compose_post(username, int(user_id_str), text)
                _ok(username, text)
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
        description="ComposePostService Python client",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",    default="127.0.0.1")
    parser.add_argument("--port",    default=9100, type=int)
    parser.add_argument("--timeout", default=10000, type=int)
    parser.add_argument("--retries", default=3,     type=int)
    parser.add_argument("-v", "--verbose", action="store_true")

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
        with ComposePostClient(host=args.host, port=args.port,
                               timeout_ms=args.timeout,
                               max_retries=args.retries) as c:
            if args.command == "compose":
                c.compose_post(
                    username=args.username,
                    user_id=args.user_id,
                    text=args.text,
                    media_ids=args.media_ids,
                    media_types=args.media_types,
                    post_type=_POST_TYPE_MAP[args.post_type],
                )
                _ok(args.username, args.text)
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