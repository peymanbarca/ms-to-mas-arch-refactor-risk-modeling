#!/usr/bin/env python3
"""
client.py — Python client for UserService

Usage examples
--------------

# Register a user
python client.py register --first Alice --last Smith --username alice --password secret123

# Login and get a JWT
python client.py login --username alice --password secret123

# Get a user's ID
python client.py get-id --username alice

# Compose a Creator struct (by username — resolves user_id automatically)
python client.py compose-creator --username alice

# Compose a Creator struct (supply user_id directly — no DB lookup)
python client.py compose-creator --username alice --user-id 42

# Custom host/port
python client.py --host 127.0.0.1 --port 9094 login --username alice --password s

# Interactive REPL
python client.py repl

Programmatic usage
------------------
from client import UserServiceClient

with UserServiceClient(host="127.0.0.1", port=9094) as c:
    c.register_user("Alice", "Smith", "alice", "secret")
    token   = c.login("alice", "secret")
    user_id = c.get_user_id("alice")
    creator = c.compose_creator_with_username("alice")
    print(creator.user_id, creator.username)
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

from ms_baseline.dsb_social.gen_py.social_network import UserService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException

logger = logging.getLogger("user-service-client")


class UserServiceClient:
    """
    Thrift client for UserService.

    Context-manager usage (recommended):

        with UserServiceClient("127.0.0.1", 9094) as c:
            c.register_user("Alice", "Smith", "alice", "password")
            token = c.login("alice", "password")

    Parameters
    ----------
    host, port      : service address
    timeout_ms      : socket timeout in ms  (default 5000)
    max_retries     : connection attempts   (default 3)
    retry_delay     : seconds between retries (default 0.5)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9094,
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
                self._client    = UserService.Client(protocol)
                logger.debug("Connected to UserService at %s:%d", self._host, self._port)
                return
            except TTransportException as exc:
                last_exc = exc
                logger.warning("Connection attempt %d/%d failed: %s",
                               attempt, self._max_retries, exc)
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
        raise ConnectionError(
            f"Could not connect to UserService at {self._host}:{self._port} "
            f"after {self._max_retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        if self._transport and self._transport.isOpen():
            self._transport.close()
        self._transport = None
        self._client    = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.isOpen()

    def __enter__(self) -> "UserServiceClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Service calls
    # ------------------------------------------------------------------

    def register_user(
        self,
        first_name: str,
        last_name: str,
        username: str,
        password: str,
        carrier: dict | None = None,
    ) -> None:
        """Register a new user (auto-generated user_id)."""
        self._ensure_connected()
        try:
            self._client.RegisterUser(
                self._next_req_id(), first_name, last_name,
                username, password, carrier or {},
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def register_user_with_id(
        self,
        first_name: str,
        last_name: str,
        username: str,
        password: str,
        user_id: int,
        carrier: dict | None = None,
    ) -> None:
        """Register a user with an explicit user_id (used by seed scripts)."""
        self._ensure_connected()
        try:
            self._client.RegisterUserWithId(
                self._next_req_id(), first_name, last_name,
                username, password, user_id, carrier or {},
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def login(
        self,
        username: str,
        password: str,
        carrier: dict | None = None,
    ) -> str:
        """Verify credentials and return a signed JWT token string."""
        self._ensure_connected()
        try:
            return self._client.Login(
                self._next_req_id(), username, password, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def compose_creator_with_user_id(
        self,
        user_id: int,
        username: str,
        carrier: dict | None = None,
    ):
        """Build a Creator struct from user_id + username (no DB lookup)."""
        self._ensure_connected()
        try:
            return self._client.ComposeCreatorWithUserId(
                self._next_req_id(), user_id, username, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def compose_creator_with_username(
        self,
        username: str,
        carrier: dict | None = None,
    ):
        """Resolve username → user_id and build a Creator struct."""
        self._ensure_connected()
        try:
            return self._client.ComposeCreatorWithUsername(
                self._next_req_id(), username, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    def get_user_id(
        self,
        username: str,
        carrier: dict | None = None,
    ) -> int:
        """Resolve username → user_id (i64)."""
        self._ensure_connected()
        try:
            return self._client.GetUserId(
                self._next_req_id(), username, carrier or {}
            )
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(str(exc)) from exc

    # Aliases matching Thrift method names exactly
    RegisterUser               = register_user
    RegisterUserWithId         = register_user_with_id
    Login                      = login
    ComposeCreatorWithUserId   = compose_creator_with_user_id
    ComposeCreatorWithUsername = compose_creator_with_username
    GetUserId                  = get_user_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected. Call connect() or use 'with UserServiceClient(...) as c:'"
            )

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def _print_creator(creator) -> None:
    print(f"\n  username : {creator.username}")
    print(f"  user_id  : {creator.user_id}\n")


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def run_repl(client: UserServiceClient) -> None:
    print("UserService REPL")
    print("Commands:")
    print("  register <username> <password> <first> <last>")
    print("  login    <username> <password>")
    print("  getid    <username>")
    print("  creator  <username>")
    print("  quit\n")

    while True:
        try:
            line = input("user-service> ").strip()
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
            elif cmd == "register":
                if len(args) < 4:
                    print("Usage: register <username> <password> <first> <last>")
                    continue
                client.register_user(args[2], args[3], args[0], args[1])
                print(f"  Registered @{args[0]}")
            elif cmd == "login":
                if len(args) < 2:
                    print("Usage: login <username> <password>")
                    continue
                token = client.login(args[0], args[1])
                print(f"  JWT: {token}")
            elif cmd == "getid":
                if not args:
                    print("Usage: getid <username>")
                    continue
                uid = client.get_user_id(args[0])
                print(f"  @{args[0]} -> user_id={uid}")
            elif cmd == "creator":
                if not args:
                    print("Usage: creator <username>")
                    continue
                c = client.compose_creator_with_username(args[0])
                _print_creator(c)
            else:
                print(f"Unknown command: {cmd!r}")
        except ServiceException as exc:
            print(f"[SERVICE ERROR] {exc.message}")
        except Exception as exc:
            print(f"[ERROR] {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s")

    parser = argparse.ArgumentParser(description="UserService Python client",
                                     epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host",    default="127.0.0.1")
    parser.add_argument("--port",    default=9094, type=int)
    parser.add_argument("--timeout", default=5000, type=int)
    parser.add_argument("--retries", default=3,    type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="Register a new user")
    p.add_argument("--first",    required=True)
    p.add_argument("--last",     required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--user-id",  type=int, default=None,
                   help="Supply an explicit user_id (optional)")

    p = sub.add_parser("login", help="Login and get JWT")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)

    p = sub.add_parser("get-id", help="Get user_id for a username")
    p.add_argument("--username", required=True)

    p = sub.add_parser("compose-creator", help="Build a Creator struct")
    p.add_argument("--username", required=True)
    p.add_argument("--user-id",  type=int, default=None,
                   help="Supply user_id directly (skips DB lookup)")

    sub.add_parser("repl", help="Interactive REPL")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        with UserServiceClient(host=args.host, port=args.port,
                               timeout_ms=args.timeout, max_retries=args.retries) as c:
            if args.command == "register":
                if args.user_id is not None:
                    c.register_user_with_id(args.first, args.last,
                                            args.username, args.password, args.user_id)
                else:
                    c.register_user(args.first, args.last, args.username, args.password)
                print(f"  Registered @{args.username}")

            elif args.command == "login":
                token = c.login(args.username, args.password)
                print(f"  JWT: {token}")

            elif args.command == "get-id":
                uid = c.get_user_id(args.username)
                print(f"  @{args.username} -> user_id={uid}")

            elif args.command == "compose-creator":
                if args.user_id is not None:
                    creator = c.compose_creator_with_user_id(args.user_id, args.username)
                else:
                    creator = c.compose_creator_with_username(args.username)
                _print_creator(creator)

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