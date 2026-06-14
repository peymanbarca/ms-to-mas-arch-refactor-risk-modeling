#!/usr/bin/env python3
"""
client.py — Python client for UserTimelineService

Usage examples
--------------

# Write a post into a user's timeline
python client.py write --user-id 1 --post-id 42 --timestamp 1717000000000

# Read timeline posts (paginated)
python client.py read --user-id 1 --start 0 --stop 10

# Custom host/port
python client.py --host 127.0.0.1 --port 9098 read --user-id 1

# Interactive REPL
python client.py repl

Programmatic usage
------------------
from client import UserTimelineClient

with UserTimelineClient(host="127.0.0.1", port=9098) as c:
    c.write_user_timeline(user_id=1, post_id=42, timestamp=1717000000000)
    posts = c.read_user_timeline(user_id=1, start=0, stop=10)
    for p in posts:
        print(p.post_id, p.text)
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

from ms_baseline.dsb_social.gen_py.social_network import UserTimelineService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException

logger = logging.getLogger("user-timeline-client")


class UserTimelineClient:
    """
    Thrift client for UserTimelineService.

    Context-manager usage (recommended):

        with UserTimelineClient("127.0.0.1", 9098) as c:
            c.write_user_timeline(1, 42, 1717000000000)
            posts = c.read_user_timeline(1, 0, 10)

    Parameters
    ----------
    host, port      : service address
    timeout_ms      : socket timeout ms    (default 20000)
    max_retries     : connection attempts  (default 3)
    retry_delay     : seconds between retries (default 0.5)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9098,
        timeout_ms: int = 20000,
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
                self._client    = UserTimelineService.Client(protocol)
                logger.debug("Connected to UserTimelineService at %s:%d",
                             self._host, self._port)
                return
            except TTransportException as exc:
                last_exc = exc
                logger.warning("Connection attempt %d/%d failed: %s",
                               attempt, self._max_retries, exc)
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
        raise ConnectionError(
            f"Could not connect to UserTimelineService at "
            f"{self._host}:{self._port} after {self._max_retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        if self._transport and self._transport.isOpen():
            self._transport.close()
        self._transport = None
        self._client    = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.isOpen()

    def __enter__(self) -> "UserTimelineClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Service calls
    # ------------------------------------------------------------------

    def write_user_timeline(
        self,
        user_id: int,
        post_id: int,
        timestamp: int,
        carrier: dict | None = None,
    ) -> None:
        """
        Record a post in the user's personal timeline.

        Parameters
        ----------
        user_id   : the author's user_id
        post_id   : the new post's ID
        timestamp : millisecond Unix timestamp of the post
        """
        self._ensure_connected()
        try:
            self._client.WriteUserTimeline(
                self._next_req_id(), post_id, user_id, timestamp, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def read_user_timeline(
        self,
        user_id: int,
        start: int = 0,
        stop: int = 10,
        carrier: dict | None = None,
    ) -> list:
        """
        Read the user's personal timeline posts (most recent first).

        Parameters
        ----------
        user_id : the user whose timeline to read
        start   : 0-based inclusive start index (default 0)
        stop    : exclusive stop index          (default 10)

        Returns
        -------
        list[Post] — hydrated Post structs
        """
        self._ensure_connected()
        try:
            return self._client.ReadUserTimeline(
                self._next_req_id(), user_id, start, stop, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    # Aliases matching Thrift method names
    WriteUserTimeline = write_user_timeline
    ReadUserTimeline  = read_user_timeline

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected. Call connect() or use "
                "'with UserTimelineClient(...) as c:'"
            )

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def _print_posts(posts: list) -> None:
    if not posts:
        print("  (no posts)")
        return
    print(f"\n  {'post_id':<20} {'timestamp':<16} text")
    print(f"  {'-'*20} {'-'*16} {'-'*40}")
    for p in posts:
        print(f"  {p.post_id:<20} {p.timestamp:<16} {p.text[:60]!r}")
    print()


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def run_repl(client: UserTimelineClient) -> None:
    print("UserTimelineService REPL")
    print("Commands:")
    print("  write <user_id> <post_id> <timestamp_ms>")
    print("  read  <user_id> [start=0] [stop=10]")
    print("  quit\n")

    while True:
        try:
            line = input("user-timeline> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]

        try:
            if cmd in ("quit", "exit", "q"):
                print("Bye!")
                break
            elif cmd == "write":
                if len(args) < 3:
                    print("Usage: write <user_id> <post_id> <timestamp_ms>")
                    continue
                client.write_user_timeline(int(args[0]), int(args[1]), int(args[2]))
                print(f"  Written post_id={args[1]} to timeline of user_id={args[0]}")
            elif cmd == "read":
                if not args:
                    print("Usage: read <user_id> [start=0] [stop=10]")
                    continue
                user_id = int(args[0])
                start   = int(args[1]) if len(args) > 1 else 0
                stop    = int(args[2]) if len(args) > 2 else 10
                posts   = client.read_user_timeline(user_id, start, stop)
                _print_posts(posts)
            else:
                print(f"Unknown command: {cmd!r}")
        except (ServiceException, ConnectionError, IndexError, ValueError) as exc:
            print(f"[ERROR] {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="UserTimelineService Python client",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",    default="127.0.0.1")
    parser.add_argument("--port",    default=9098, type=int)
    parser.add_argument("--timeout", default=30000, type=int)
    parser.add_argument("--retries", default=3,    type=int)
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("write", help="Write a post into a user's timeline")
    p.add_argument("--user-id",   type=int,   required=True)
    p.add_argument("--post-id",   type=int,   required=True)
    p.add_argument("--timestamp", type=int,
                   default=None, help="ms timestamp (default: now)")

    p = sub.add_parser("read", help="Read a user's timeline")
    p.add_argument("--user-id", type=int, required=True)
    p.add_argument("--start",   type=int, default=0)
    p.add_argument("--stop",    type=int, default=10)

    sub.add_parser("repl", help="Interactive REPL")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        with UserTimelineClient(host=args.host, port=args.port,
                                timeout_ms=args.timeout,
                                max_retries=args.retries) as c:
            if args.command == "write":
                ts = args.timestamp or int(time.time() * 1000)
                c.write_user_timeline(args.user_id, args.post_id, ts)
                print(f"  Written post_id={args.post_id} for user_id={args.user_id}")
            elif args.command == "read":
                posts = c.read_user_timeline(args.user_id, args.start, args.stop)
                _print_posts(posts)
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
