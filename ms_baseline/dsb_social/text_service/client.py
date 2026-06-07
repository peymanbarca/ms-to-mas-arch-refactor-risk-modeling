#!/usr/bin/env python3
"""
client.py — Python client for TextService

Usage examples
--------------

# Compose text (parse URLs and @mentions)
python client.py compose "Hello @alice, check https://example.com/page"

# Custom host/port
python client.py --host 127.0.0.1 --port 9095 compose "some text"

# Interactive REPL
python client.py repl

Programmatic usage
------------------
from client import TextServiceClient

with TextServiceClient(host="127.0.0.1", port=9095) as c:
    result = c.compose_text("Hello @alice, visit https://example.com")
    print(result.text)            # text with shortened URLs
    print(result.user_mentions)   # list of UserMention
    print(result.urls)            # list of Url
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

from ms_baseline.dsb_social.gen_py.social_network import TextService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException

logger = logging.getLogger("text-service-client")


class TextServiceClient:
    """
    Thrift client for TextService.

    Context-manager usage (recommended):

        with TextServiceClient("127.0.0.1", 9095) as c:
            result = c.compose_text("Hello @alice, visit https://example.com")

    Parameters
    ----------
    host, port      : service address
    timeout_ms      : socket timeout ms   (default 5000)
    max_retries     : connection attempts (default 3)
    retry_delay     : seconds between retries (default 0.5)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9095,
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
                self._client    = TextService.Client(protocol)
                logger.debug("Connected to TextService at %s:%d", self._host, self._port)
                return
            except TTransportException as exc:
                last_exc = exc
                logger.warning("Connection attempt %d/%d failed: %s",
                               attempt, self._max_retries, exc)
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
        raise ConnectionError(
            f"Could not connect to TextService at {self._host}:{self._port} "
            f"after {self._max_retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        if self._transport and self._transport.isOpen():
            self._transport.close()
        self._transport = None
        self._client    = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.isOpen()

    def __enter__(self) -> "TextServiceClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Service call
    # ------------------------------------------------------------------

    def compose_text(
        self,
        text: str,
        carrier: dict | None = None,
    ):
        """
        Parse URLs and @mentions in text, call downstream services, and
        return a TextServiceReturn with the processed result.

        Parameters
        ----------
        text    : raw post text
        carrier : OpenTracing propagation headers (optional)

        Returns
        -------
        TextServiceReturn with fields:
          .text          — modified text (URLs replaced with shortened forms)
          .user_mentions — list[UserMention] (user_id + username)
          .urls          — list[Url] (shortened_url + expanded_url)

        Raises
        ------
        ServiceException  — propagated from UrlShortenService or UserMentionService
        ConnectionError   — transport-level failure
        """
        self._ensure_connected()
        req_id  = self._next_req_id()
        carrier = carrier or {}
        logger.debug("ComposeText req_id=%d text_len=%d", req_id, len(text))
        try:
            result = self._client.ComposeText(req_id, text, carrier)
            logger.debug("ComposeText returned text_len=%d", len(result.text))
            return result
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(f"Transport error during ComposeText: {exc}") from exc

    # Alias matching the Thrift method name
    ComposeText = compose_text

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected. Call connect() or use 'with TextServiceClient(...) as c:'"
            )

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def _print_result(result) -> None:
    print(f"\n  Text      : {result.text}")
    if result.urls:
        print("  URLs      :")
        for u in result.urls:
            print(f"    {u.expanded_url}  ->  {u.shortened_url}")
    else:
        print("  URLs      : (none)")
    if result.user_mentions:
        print("  Mentions  :")
        for m in result.user_mentions:
            print(f"    @{m.username}  ->  user_id={m.user_id}")
    else:
        print("  Mentions  : (none)")
    print()


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def run_repl(client: TextServiceClient) -> None:
    print("TextService REPL")
    print("Commands:")
    print("  compose <text>   — process text (parse URLs and @mentions)")
    print("  quit             — quit\n")

    while True:
        try:
            line = input("text-service> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not line:
            continue
        parts = line.split(None, 1)
        cmd   = parts[0].lower()
        rest  = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            print("Bye!")
            break
        elif cmd in ("compose", "c"):
            if not rest:
                print("Usage: compose <text>")
                continue
            try:
                result = client.compose_text(rest)
                _print_result(result)
            except ServiceException as exc:
                print(f"[SERVICE ERROR] {exc.message}")
            except Exception as exc:
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
        description="TextService Python client",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",    default="127.0.0.1")
    parser.add_argument("--port",    default=9095, type=int)
    parser.add_argument("--timeout", default=5000, type=int)
    parser.add_argument("--retries", default=3,    type=int)
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("compose", help="Compose (process) a text string")
    p.add_argument("text", help="Raw post text to process")

    sub.add_parser("repl", help="Interactive REPL")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        with TextServiceClient(host=args.host, port=args.port,
                               timeout_ms=args.timeout,
                               max_retries=args.retries) as c:
            if args.command == "compose":
                result = c.compose_text(args.text)
                _print_result(result)
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
