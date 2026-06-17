"""
thrift_pool.py — Thread-safe Thrift client connection pool.

Identical pattern used across text-service, social-graph-service,
user-timeline-service, etc. Used here for PostStorageService and
SocialGraphService downstream clients.
"""

import contextlib
import logging
import queue

from thrift.transport import TSocket, TTransport
from thrift.protocol  import TBinaryProtocol

logger = logging.getLogger("home-timeline-service.pool")


class ThriftClientPool:
    """
    A bounded pool of Thrift clients for a single downstream service.

    Parameters
    ----------
    client_class : generated Thrift Client class
    host         : downstream service host
    port         : downstream service port
    size         : pool capacity (default 16)
    timeout_ms   : socket timeout in ms (default 5000)
    """

    def __init__(self, client_class, host: str, port: int,
                 size: int = 16, timeout_ms: int = 5000):
        self._client_class = client_class
        self._host         = host
        self._port         = port
        self._timeout_ms   = timeout_ms
        self._pool: queue.Queue = queue.Queue(maxsize=size)
        for _ in range(size):
            self._pool.put(None)

    @contextlib.contextmanager
    def connection(self):
        slot = self._pool.get()
        client, transport = self._ensure_open(slot)
        try:
            yield client
            self._pool.put((client, transport))
        except Exception:
            self._try_close(transport)
            self._pool.put(None)
            raise

    def _ensure_open(self, slot):
        if slot is None:
            return self._open_connection()
        client, transport = slot
        if transport.isOpen():
            return client, transport
        self._try_close(transport)
        return self._open_connection()

    def _open_connection(self):
        sock      = TSocket.TSocket(self._host, self._port)
        sock.setTimeout(self._timeout_ms)
        transport = TTransport.TFramedTransport(sock)
        protocol  = TBinaryProtocol.TBinaryProtocol(transport)
        transport.open()
        client    = self._client_class(protocol)
        logger.debug("Opened connection to %s:%d", self._host, self._port)
        return client, transport

    @staticmethod
    def _try_close(transport):
        try:
            if transport and transport.isOpen():
                transport.close()
        except Exception:
            pass