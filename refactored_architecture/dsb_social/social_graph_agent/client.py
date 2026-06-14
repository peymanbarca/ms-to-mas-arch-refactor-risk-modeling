#!/usr/bin/env python3
"""
client.py — Python client for SocialGraphService

Usage examples
--------------

# Insert a user into the graph
python client.py insert-user --user-id 1

# Follow by user_id
python client.py follow --user-id 1 --followee-id 2

# Follow by username (UserService resolves the IDs)
python client.py follow-username --username alice --followee alice_friend

# Unfollow
python client.py unfollow --user-id 1 --followee-id 2

# Get followers
python client.py get-followers --user-id 2

# Get followees
python client.py get-followees --user-id 1

# Interactive REPL
python client.py repl

Programmatic usage
------------------
from client import SocialGraphClient

with SocialGraphClient(host="127.0.0.1", port=9097) as c:
    c.insert_user(1)
    c.follow(1, 2)
    followers = c.get_followers(2)
    followees = c.get_followees(1)
    c.unfollow(1, 2)
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

from ms_baseline.dsb_social.gen_py.social_network import SocialGraphService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException

logger = logging.getLogger("social-graph-client")


class SocialGraphClient:
    """
    Thrift client for SocialGraphService.

    Context-manager usage (recommended):

        with SocialGraphClient("127.0.0.1", 9097) as c:
            c.follow(1, 2)
            followers = c.get_followers(2)

    Parameters
    ----------
    host, port      : service address
    timeout_ms      : socket timeout in ms   (default 20000)
    max_retries     : connection attempts    (default 3)
    retry_delay     : seconds between retries (default 0.5)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9097,
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
                self._client    = SocialGraphService.Client(protocol)
                logger.debug("Connected to SocialGraphService at %s:%d",
                             self._host, self._port)
                return
            except TTransportException as exc:
                last_exc = exc
                logger.warning("Connection attempt %d/%d failed: %s",
                               attempt, self._max_retries, exc)
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
        raise ConnectionError(
            f"Could not connect to SocialGraphService at {self._host}:{self._port} "
            f"after {self._max_retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        if self._transport and self._transport.isOpen():
            self._transport.close()
        self._transport = None
        self._client    = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.isOpen()

    def __enter__(self) -> "SocialGraphClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Service calls
    # ------------------------------------------------------------------

    def insert_user(self, user_id: int, carrier: dict | None = None) -> None:
        """Initialise an empty graph entry for a new user."""
        self._ensure_connected()
        try:
            self._client.InsertUser(self._next_req_id(), user_id, carrier or {})
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def follow(
        self, user_id: int, followee_id: int, carrier: dict | None = None
    ) -> None:
        """user_id starts following followee_id."""
        self._ensure_connected()
        try:
            self._client.Follow(
                self._next_req_id(), user_id, followee_id, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def unfollow(
        self, user_id: int, followee_id: int, carrier: dict | None = None
    ) -> None:
        """user_id stops following followee_id."""
        self._ensure_connected()
        try:
            self._client.Unfollow(
                self._next_req_id(), user_id, followee_id, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def follow_with_username(
        self,
        user_username: str,
        followee_username: str,
        carrier: dict | None = None,
    ) -> None:
        """Resolve usernames and follow (delegates to UserService)."""
        self._ensure_connected()
        try:
            self._client.FollowWithUsername(
                self._next_req_id(), user_username, followee_username, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def unfollow_with_username(
        self,
        user_username: str,
        followee_username: str,
        carrier: dict | None = None,
    ) -> None:
        """Resolve usernames and unfollow."""
        self._ensure_connected()
        try:
            self._client.UnfollowWithUsername(
                self._next_req_id(), user_username, followee_username, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def get_followers(
        self, user_id: int, carrier: dict | None = None
    ) -> list[int]:
        """Return list of user_ids that follow user_id."""
        self._ensure_connected()
        try:
            return self._client.GetFollowers(
                self._next_req_id(), user_id, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def get_followees(
        self, user_id: int, carrier: dict | None = None
    ) -> list[int]:
        """Return list of user_ids that user_id follows."""
        self._ensure_connected()
        try:
            return self._client.GetFollowees(
                self._next_req_id(), user_id, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    # Aliases matching Thrift method names
    InsertUser          = insert_user
    Follow              = follow
    Unfollow            = unfollow
    FollowWithUsername  = follow_with_username
    UnfollowWithUsername = unfollow_with_username
    GetFollowers        = get_followers
    GetFollowees        = get_followees

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected. Call connect() or use "
                "'with SocialGraphClient(...) as c:'"
            )

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def _print_ids(label: str, ids: list) -> None:
    print(f"\n  {label}: {ids if ids else '(none)'}\n")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_repl(client: SocialGraphClient) -> None:
    print("SocialGraphService REPL")
    print("Commands:")
    print("  insert   <user_id>")
    print("  follow   <user_id> <followee_id>")
    print("  unfollow <user_id> <followee_id>")
    print("  followers <user_id>")
    print("  followees <user_id>")
    print("  quit\n")

    while True:
        try:
            line = input("social-graph> ").strip()
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
            elif cmd == "insert":
                client.insert_user(int(args[0]))
                print(f"  Inserted user_id={args[0]}")
            elif cmd == "follow":
                client.follow(int(args[0]), int(args[1]))
                print(f"  {args[0]} now follows {args[1]}")
            elif cmd == "unfollow":
                client.unfollow(int(args[0]), int(args[1]))
                print(f"  {args[0]} unfollowed {args[1]}")
            elif cmd in ("followers", "get-followers"):
                ids = client.get_followers(int(args[0]))
                _print_ids(f"Followers of {args[0]}", ids)
            elif cmd in ("followees", "get-followees"):
                ids = client.get_followees(int(args[0]))
                _print_ids(f"Followees of {args[0]}", ids)
            else:
                print(f"Unknown: {cmd!r}")
        except (ServiceException, ConnectionError, IndexError, ValueError) as exc:
            print(f"[ERROR] {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="SocialGraphService Python client",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",    default="127.0.0.1")
    parser.add_argument("--port",    default=9097, type=int)
    parser.add_argument("--timeout", default=20000, type=int)
    parser.add_argument("--retries", default=3,    type=int)
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("insert-user")
    p.add_argument("--user-id", type=int, required=True)

    p = sub.add_parser("follow")
    p.add_argument("--user-id",     type=int, required=True)
    p.add_argument("--followee-id", type=int, required=True)

    p = sub.add_parser("unfollow")
    p.add_argument("--user-id",     type=int, required=True)
    p.add_argument("--followee-id", type=int, required=True)

    p = sub.add_parser("follow-username")
    p.add_argument("--username",          required=True)
    p.add_argument("--followee-username", required=True)

    p = sub.add_parser("unfollow-username")
    p.add_argument("--username",          required=True)
    p.add_argument("--followee-username", required=True)

    p = sub.add_parser("get-followers")
    p.add_argument("--user-id", type=int, required=True)

    p = sub.add_parser("get-followees")
    p.add_argument("--user-id", type=int, required=True)

    sub.add_parser("repl")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        with SocialGraphClient(host=args.host, port=args.port,
                               timeout_ms=args.timeout,
                               max_retries=args.retries) as c:
            if args.command == "insert-user":
                c.insert_user(args.user_id)
                print(f"  Inserted user_id={args.user_id}")
            elif args.command == "follow":
                c.follow(args.user_id, args.followee_id)
                print(f"  {args.user_id} now follows {args.followee_id}")
            elif args.command == "unfollow":
                c.unfollow(args.user_id, args.followee_id)
                print(f"  {args.user_id} unfollowed {args.followee_id}")
            elif args.command == "follow-username":
                c.follow_with_username(args.username, args.followee_username)
                print(f"  @{args.username} now follows @{args.followee_username}")
            elif args.command == "unfollow-username":
                c.unfollow_with_username(args.username, args.followee_username)
                print(f"  @{args.username} unfollowed @{args.followee_username}")
            elif args.command == "get-followers":
                ids = c.get_followers(args.user_id)
                _print_ids(f"Followers of {args.user_id}", ids)
            elif args.command == "get-followees":
                ids = c.get_followees(args.user_id)
                _print_ids(f"Followees of {args.user_id}", ids)
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
