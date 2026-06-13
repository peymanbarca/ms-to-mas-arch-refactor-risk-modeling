"""
thrift_pool.py — Thread-safe Thrift client connection pool.

The C++ TextHandler uses ClientPool<ThriftClient<T>> for both downstream
services (UrlShortenService and UserMentionService).  This module provides
a lightweight Python equivalent: a fixed-size pool of pre-opened Thrift
connections, with blocking acquire/release semantics.

Pool behaviour:
  - Connections are created lazily on first acquire.
  - A released connection is returned to the pool for reuse.
  - If a connection is broken on release it is discarded; the next acquire
    opens a fresh one.
  - acquire() blocks until a connection is available (matching C++ behaviour).
"""

import contextlib
import logging
import queue
import threading
from typing import Callable

from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol

logger = logging.getLogger("text-service.pool")


class ThriftClientPool:
    """
    A bounded pool of Thrift clients for a single downstream service.

    Parameters
    ----------
    client_class  : the generated Thrift Client class (e.g. UrlShortenService.Client)
    host          : downstream service host
    port          : downstream service port
    size          : pool capacity (default 8, matching C++ ClientPool default)
    timeout_ms    : socket timeout in ms (default 5000)
    """

    def __init__(
        self,
        client_class,
        host: str,
        port: int,
        size: int = 8,
        timeout_ms: int = 5000,
    ):
        self._client_class = client_class
        self._host         = host
        self._port         = port
        self._timeout_ms   = timeout_ms
        self._pool: queue.Queue = queue.Queue(maxsize=size)
        self._size         = size

        # Pre-fill with None sentinels; actual connections created lazily
        for _ in range(size):
            self._pool.put(None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def connection(self):
        """
        Context manager that yields a ready Thrift client.

        Usage:
            with pool.connection() as client:
                result = client.SomeMethod(...)
        """
        slot = self._pool.get()       # blocks until a slot is available
        client, transport = self._ensure_open(slot)
        try:
            yield client
            # Return healthy connection to pool
            self._pool.put((client, transport))
        except Exception:
            # Connection may be broken — discard it; put a fresh None slot back
            self._try_close(transport)
            self._pool.put(None)
            raise

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_open(self, slot):
        """Return (client, transport), opening a new connection if slot is None."""
        if slot is None:
            return self._open_connection()

        client, transport = slot
        if transport.isOpen():
            return client, transport

        # Transport was closed — reopen
        self._try_close(transport)
        return self._open_connection()

    def _open_connection(self):
        sock      = TSocket.TSocket(self._host, self._port)
        sock.setTimeout(self._timeout_ms)
        transport = TTransport.TFramedTransport(sock)
        protocol  = TBinaryProtocol.TBinaryProtocol(transport)
        transport.open()
        client = self._client_class(protocol)
        logger.debug("Opened new connection to %s:%d", self._host, self._port)
        return client, transport

    @staticmethod
    def _try_close(transport):
        try:
            if transport and transport.isOpen():
                transport.close()
        except Exception:
            pass
