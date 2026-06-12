#!/usr/bin/env python3
"""
client.py — Python client for UserMentionService (Agent version)

Usage examples
--------------

# Resolve single username
python client.py resolve alice

# Resolve multiple usernames
python client.py resolve alice bob charlie

# Custom host/port
python client.py --host 127.0.0.1 --port 9093 resolve alice

# Interactive REPL
python client.py repl

Programmatic usage
------------------
from client import UserMentionClient

with UserMentionClient(host="127.0.0.1", port=9093) as c:
    mentions = c.compose_user_mentions(["alice", "bob"])
    for m in mentions:
        print(f"@{m.username}  ->  user_id={m.user_id}")
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

from ms_baseline.dsb_social.gen_py.social_network import UserMentionService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException

logger = logging.getLogger("user-mention-agent.client")


class UserMentionClient:
    """
    Thrift client for UserMentionService.

    Supports context manager usage:

        with UserMentionClient("127.0.0.1", 9093) as c:
            mentions = c.compose_user_mentions(["alice", "bob"])

    Or manual open/close:

        c = UserMentionClient()
        c.connect()
        mentions = c.compose_user_mentions(["alice"])
        c.close()

    Parameters
    ----------
    host        : Service host            (default "127.0.0.1")
    port        : Service port            (default 9093)
    timeout_ms  : Socket timeout in ms    (default 5000)
    max_retries : Connection attempts     (default 3)
    retry_delay : Seconds between retries (default 0.5)
    req_id      : Starting request ID — auto-incremented per call
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9093,
        timeout_ms: int = 5000,
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

        self._transport = None
        self._client    = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open transport with retry."""
        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                sock = TSocket.TSocket(self._host, self._port)
                sock.setTimeout(self._timeout_ms)
                transport = TTransport.TFramedTransport(sock)
                protocol  = TBinaryProtocol.TBinaryProtocol(transport)
                transport.open()
                self._transport = transport
                self._client    = UserMentionService.Client(protocol)
                logger.debug(
                    "Connected to UserMentionService at %s:%d",
                    self._host, self._port,
                )
                return
            except TTransportException as exc:
                last_exc = exc
                logger.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt, self._max_retries, exc,
                )
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)

        raise ConnectionError(
            f"Could not connect to UserMentionService at "
            f"{self._host}:{self._port} after {self._max_retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        if self._transport and self._transport.isOpen():
            self._transport.close()
            logger.debug("Disconnected from UserMentionService")
        self._transport = None
        self._client    = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.isOpen()

    def __enter__(self) -> "UserMentionClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Service call
    # ------------------------------------------------------------------

    def compose_user_mentions(
        self,
        usernames: list[str],
        carrier: dict | None = None,
    ) -> list:
        """
        Resolve @mention usernames to UserMention structs.

        Parameters
        ----------
        usernames : list of username strings (without the '@')
        carrier   : OpenTracing propagation headers (optional)

        Returns
        -------
        list[UserMention]  — each item has .user_id (i64) and .username (str)

        Raises
        ------
        ServiceException  — if any username is not found in the system
        ConnectionError   — transport-level failure
        """
        self._ensure_connected()
        req_id  = self._next_req_id()
        carrier = carrier or {}
        logger.debug(
            "ComposeUserMentions req_id=%d usernames=%s", req_id, usernames
        )
        try:
            result = self._client.ComposeUserMentions(req_id, usernames, carrier)
            logger.debug(
                "ComposeUserMentions returned %d mentions", len(result)
            )
            return result
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(
                f"Transport error during ComposeUserMentions: {exc}"
            ) from exc

    # Alias matching the Thrift method name exactly
    ComposeUserMentions = compose_user_mentions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected. Call connect() or use "
                "'with UserMentionClient(...) as c:'"
            )

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id


# ---------------------------------------------------------------------------
# Pretty-printing helpers
# ---------------------------------------------------------------------------

def _print_mentions(mentions: list) -> None:
    if not mentions:
        print("  (no results)")
        return
    width = max(len(m.username) for m in mentions)
    print(f"\n  {'username':<{width}}   user_id")
    print(f"  {'-'*width}   {'-'*18}")
    for m in mentions:
        print(f"  @{m.username:<{width-1}}   {m.user_id}")
    print()


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def run_repl(client: UserMentionClient) -> None:
    print("UserMentionService REPL")
    print("Commands:")
    print("  resolve <username> [username2 ...]  — resolve @mentions to user IDs")
    print("  quit / exit                         — quit\n")

    while True:
        try:
            line = input("user-mention> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not line:
            continue

        parts = line.split()
        cmd   = parts[0].lower()
        args  = parts[1:]

        if cmd in ("quit", "exit", "q"):
            print("Bye!")
            break

        elif cmd in ("resolve", "r"):
            if not args:
                print("Usage: resolve <username> [username2 ...]")
                continue
            try:
                mentions = client.compose_user_mentions(args)
                _print_mentions(mentions)
            except ServiceException as exc:
                print(f"[SERVICE ERROR] {exc.message}")
            except Exception as exc:
                print(f"[ERROR] {exc}")

        else:
            print(f"Unknown command: {cmd!r}. Try 'resolve' or 'quit'.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="UserMentionService Agent — Python client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",    default="127.0.0.1", help="Service host")
    parser.add_argument("--port",    default=9093, type=int, help="Service port")
    parser.add_argument("--timeout", default=5000, type=int, help="Socket timeout ms")
    parser.add_argument("--retries", default=3,    type=int, help="Max retries")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p_resolve = sub.add_parser("resolve", help="Resolve usernames to user IDs")
    p_resolve.add_argument("usernames", nargs="+", metavar="USERNAME")

    sub.add_parser("repl", help="Interactive REPL")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        with UserMentionClient(
            host=args.host,
            port=args.port,
            timeout_ms=args.timeout,
            max_retries=args.retries,
        ) as client:
            if args.command == "resolve":
                mentions = client.compose_user_mentions(args.usernames)
                _print_mentions(mentions)
            elif args.command == "repl":
                run_repl(client)

    except ConnectionError as exc:
        print(f"[CONNECTION ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except ServiceException as exc:
        print(f"[SERVICE ERROR] {exc.message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
