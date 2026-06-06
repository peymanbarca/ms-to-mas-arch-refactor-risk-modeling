#!/usr/bin/env python3
"""
client.py — Python client for UrlShortenService

Usage examples
--------------

# One-shot shorten
python client.py shorten "https://www.example.com/some/very/long/path"

# Shorten multiple URLs
python client.py shorten "https://example.com/a" "https://example.com/b"

# Expand a shortened URL
python client.py expand "http://short-url/Ab3Kp9mXzQ"

# Expand multiple
python client.py expand "http://short-url/Ab3Kp9mXzQ" "http://short-url/Xy7Lq2nWrT"

# Custom host/port
python client.py --host 127.0.0.1 --port 9092 shorten "https://example.com"

# Interactive REPL
python client.py repl

Programmatic usage
------------------
from client import UrlShortenClient

with UrlShortenClient(host="127.0.0.1", port=9092) as client:
    urls = client.compose_urls(["https://example.com/page1", "https://example.com/page2"])
    for url in urls:
        print(f"{url.expanded_url}  ->  {url.shortened_url}")

    expanded = client.get_extended_urls([urls[0].shortened_url])
    print(expanded)
"""

import argparse
import sys
import os
import time
import random
import logging

# Make gen-py importable when running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "gen-py"))

from thrift.transport import TSocket, TTransport
from thrift.transport.TTransport import TTransportException
from thrift.protocol  import TBinaryProtocol

from ms_baseline.dsb_social.gen_py.social_network import UrlShortenService
from ms_baseline.dsb_social.gen_py.social_network.ttypes import ServiceException

logger = logging.getLogger("url-shorten-client")


# ---------------------------------------------------------------------------
# Client class
# ---------------------------------------------------------------------------

class UrlShortenClient:
    """
    Thrift client for UrlShortenService.

    Supports use as a context manager:

        with UrlShortenClient("127.0.0.1", 9092) as c:
            results = c.compose_urls(["https://example.com"])

    Or manual open/close:

        c = UrlShortenClient("127.0.0.1", 9092)
        c.connect()
        results = c.compose_urls(["https://example.com"])
        c.close()

    Parameters
    ----------
    host        : Thrift server host (default "127.0.0.1")
    port        : Thrift server port (default 9092)
    timeout_ms  : Socket timeout in milliseconds (default 5000)
    max_retries : Number of connection attempts before raising (default 3)
    retry_delay : Seconds between retries (default 0.5)
    req_id      : Starting request ID — auto-incremented per call (default random)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9092,
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

        self._transport: TTransport.TFramedTransport | None = None
        self._client:    UrlShortenService.Client    | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the Thrift transport. Retries up to max_retries times."""
        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                sock = TSocket.TSocket(self._host, self._port)
                sock.setTimeout(self._timeout_ms)
                transport = TTransport.TFramedTransport(sock)
                protocol  = TBinaryProtocol.TBinaryProtocol(transport)
                transport.open()
                self._transport = transport
                self._client    = UrlShortenService.Client(protocol)
                logger.debug(
                    "Connected to UrlShortenService at %s:%d", self._host, self._port
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
            f"Could not connect to UrlShortenService at "
            f"{self._host}:{self._port} after {self._max_retries} attempts: {last_exc}"
        )

    def close(self) -> None:
        """Close the Thrift transport."""
        if self._transport and self._transport.isOpen():
            self._transport.close()
            logger.debug("Disconnected from UrlShortenService")
        self._transport = None
        self._client    = None

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.isOpen()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "UrlShortenClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Service calls
    # ------------------------------------------------------------------

    def compose_urls(self, urls: list[str], carrier: dict | None = None) -> list:
        """
        Shorten a list of expanded URLs.

        Parameters
        ----------
        urls    : list of original long URLs
        carrier : OpenTracing propagation headers (optional)

        Returns
        -------
        list[Url]  — each item has .shortened_url and .expanded_url

        Raises
        ------
        ServiceException  — from the server (e.g. MongoDB error)
        ConnectionError   — if not connected
        """
        self._ensure_connected()
        req_id  = self._next_req_id()
        carrier = carrier or {}
        logger.debug("ComposeUrls req_id=%d urls=%s", req_id, urls)

        try:
            result = self._client.ComposeUrls(req_id, urls, carrier)
            logger.debug("ComposeUrls returned %d items", len(result))
            return result
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(f"Transport error during ComposeUrls: {exc}") from exc

    def get_extended_urls(
        self, shortened_urls: list[str], carrier: dict | None = None
    ) -> list[str]:
        """
        Expand a list of shortened URLs back to their originals.

        Parameters
        ----------
        shortened_urls : list of shortened URL strings
        carrier        : OpenTracing propagation headers (optional)

        Returns
        -------
        list[str]  — expanded URLs, same order as input

        Raises
        ------
        ServiceException  — if a shortened_url is not found
        ConnectionError   — if not connected
        """
        self._ensure_connected()
        req_id  = self._next_req_id()
        carrier = carrier or {}
        logger.debug("GetExtendedUrls req_id=%d urls=%s", req_id, shortened_urls)

        try:
            result = self._client.GetExtendedUrls(req_id, shortened_urls, carrier)
            logger.debug("GetExtendedUrls returned %d items", len(result))
            return result
        except ServiceException:
            raise
        except TTransportException as exc:
            raise ConnectionError(f"Transport error during GetExtendedUrls: {exc}") from exc

    # Convenience aliases matching the Thrift method names
    ComposeUrls      = compose_urls
    GetExtendedUrls  = get_extended_urls

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected. Call connect() or use 'with UrlShortenClient(...) as c:'"
            )

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id


# ---------------------------------------------------------------------------
# Pretty-printing helpers
# ---------------------------------------------------------------------------

def _print_compose_result(results: list) -> None:
    width = max((len(r.expanded_url) for r in results), default=0)
    print(f"\n{'Expanded URL':<{width}}  {'Shortened URL'}")
    print("-" * (width + 30))
    for r in results:
        print(f"{r.expanded_url:<{width}}  {r.shortened_url}")
    print()


def _print_expand_result(shortened: list[str], expanded: list[str]) -> None:
    width = max((len(s) for s in shortened), default=0)
    print(f"\n{'Shortened URL':<{width}}  {'Expanded URL'}")
    print("-" * (width + 60))
    for s, e in zip(shortened, expanded):
        print(f"{s:<{width}}  {e}")
    print()


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def run_repl(client: "UrlShortenClient") -> None:
    print("UrlShortenService REPL")
    print("Commands:")
    print("  shorten <url> [url2 ...]  — shorten one or more URLs")
    print("  expand  <url> [url2 ...]  — expand one or more short URLs")
    print("  quit / exit               — quit\n")

    while True:
        try:
            line = input("url-shorten> ").strip()
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

        elif cmd in ("shorten", "s"):
            if not args:
                print("Usage: shorten <url> [url2 ...]")
                continue
            try:
                results = client.compose_urls(args)
                _print_compose_result(results)
            except ServiceException as exc:
                print(f"[ERROR] {exc.message}")
            except Exception as exc:
                print(f"[ERROR] {exc}")

        elif cmd in ("expand", "e"):
            if not args:
                print("Usage: expand <short-url> [short-url2 ...]")
                continue
            try:
                results = client.get_extended_urls(args)
                _print_expand_result(args, results)
            except ServiceException as exc:
                print(f"[ERROR] {exc.message}")
            except Exception as exc:
                print(f"[ERROR] {exc}")

        else:
            print(f"Unknown command: {cmd!r}. Try 'shorten', 'expand', or 'quit'.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="UrlShortenService Python client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",    default="127.0.0.1", help="Service host (default: 127.0.0.1)")
    parser.add_argument("--port",    default=9092, type=int, help="Service port (default: 9092)")
    parser.add_argument("--timeout", default=5000, type=int, help="Socket timeout ms (default: 5000)")
    parser.add_argument("--retries", default=3,    type=int, help="Max connection retries (default: 3)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    # shorten sub-command
    p_shorten = sub.add_parser("shorten", help="Shorten one or more URLs")
    p_shorten.add_argument("urls", nargs="+", metavar="URL", help="URLs to shorten")

    # expand sub-command
    p_expand = sub.add_parser("expand", help="Expand one or more shortened URLs")
    p_expand.add_argument("urls", nargs="+", metavar="SHORT_URL", help="Shortened URLs to expand")

    # repl sub-command
    sub.add_parser("repl", help="Start an interactive REPL")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        with UrlShortenClient(
            host=args.host,
            port=args.port,
            timeout_ms=args.timeout,
            max_retries=args.retries,
        ) as client:

            if args.command == "shorten":
                results = client.compose_urls(args.urls)
                _print_compose_result(results)

            elif args.command == "expand":
                results  = client.get_extended_urls(args.urls)
                _print_expand_result(args.urls, results)

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