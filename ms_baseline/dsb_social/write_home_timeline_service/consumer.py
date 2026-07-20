"""
consumer.py — RabbitMQ consumer for WriteHomeTimelineService.

Faithful port of the C++ AMQP-CPP consumer setup in
WriteHomeTimelineService.cpp / WriteHomeTimelineHandler.h.

What the C++ original does
--------------------------
- Uses AMQP-CPP to connect to RabbitMQ.
- Declares a durable queue "write-home-timeline".
- Sets QoS prefetch to num_workers (one unacked message per worker thread).
- Starts a blocking event loop on each worker thread.
- On each delivery: calls the handler callback, then ACKs.
- On error: NACKs with requeue=True so the message is not lost.

Python implementation choices
------------------------------
- pika (pure-Python AMQP client) — same semantics as AMQP-CPP.
- BlockingConnection per worker thread — matches the C++ one-channel-per-thread
  model. Each thread has its own connection so there's no pika thread-safety
  issue (pika connections are not thread-safe).
- basic_qos prefetch_count=1 per channel — each worker processes one message
  at a time, ACKing before accepting the next (same as C++).
- Reconnect loop with exponential backoff — AMQP-CPP has similar reconnect
  logic via the event loop restart.
"""

import logging
import threading
import time

import pika
import pika.exceptions

from .worker import MessageWorker

logger = logging.getLogger("write-home-timeline-service.consumer")
logging.getLogger("pika").setLevel(logging.WARNING)

# Backoff settings for reconnect loop
_RECONNECT_INITIAL_DELAY = 1.0    # seconds
_RECONNECT_MAX_DELAY     = 30.0   # seconds
_RECONNECT_MULTIPLIER    = 2.0


class WriteHomeTimelineConsumer:
    """
    Multi-threaded RabbitMQ consumer for the write-home-timeline queue.

    Each worker thread maintains its own pika BlockingConnection and
    channel, matching the C++ one-channel-per-thread model.

    Parameters
    ----------
    rabbitmq_cfg    : dict with host/port/username/password/prefetch_count
    queue_name      : AMQP queue name (default "write-home-timeline")
    worker          : MessageWorker instance (shared across threads, thread-safe)
    num_workers     : number of consumer threads (default 4)
    """

    def __init__(
        self,
        rabbitmq_cfg: dict,
        queue_name: str,
        worker: MessageWorker,
        num_workers: int = 4,
    ):
        self._cfg         = rabbitmq_cfg
        self._queue       = queue_name
        self._worker      = worker
        self._num_workers = num_workers
        self._stop_event  = threading.Event()
        self._threads:    list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all consumer worker threads (non-blocking)."""
        logger.info(
            "Starting %d consumer thread(s) on queue=%s host=%s:%s",
            self._num_workers,
            self._queue,
            self._cfg.get("host"),
            self._cfg.get("port"),
        )
        for i in range(self._num_workers):
            t = threading.Thread(
                target=self._run_worker_loop,
                args=(i,),
                name=f"whtl-consumer-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """Signal all threads to stop and wait for them to finish."""
        logger.info("Stopping WriteHomeTimelineConsumer…")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=10)

    def join(self) -> None:
        """Block until all threads finish (use after start() in the main thread)."""
        for t in self._threads:
            t.join()

    # ------------------------------------------------------------------
    # Per-thread consume loop
    # ------------------------------------------------------------------

    def _run_worker_loop(self, worker_id: int) -> None:
        """
        Outer reconnect loop for a single worker thread.
        Restarts the connection whenever RabbitMQ drops it.
        """
        delay = _RECONNECT_INITIAL_DELAY
        while not self._stop_event.is_set():
            try:
                self._consume_blocking(worker_id)
                delay = _RECONNECT_INITIAL_DELAY   # reset on clean exit
            except pika.exceptions.AMQPConnectionError as exc:
                logger.warning(
                    "Worker %d: AMQP connection error: %s — reconnecting in %.1fs",
                    worker_id, exc, delay,
                )
            except Exception as exc:
                logger.error(
                    "Worker %d: unexpected error: %s — reconnecting in %.1fs",
                    worker_id, exc, delay,
                )

            if self._stop_event.is_set():
                break
            time.sleep(delay)
            delay = min(delay * _RECONNECT_MULTIPLIER, _RECONNECT_MAX_DELAY)

        logger.info("Worker %d: stopped", worker_id)

    def _consume_blocking(self, worker_id: int) -> None:
        """
        Open one pika BlockingConnection + channel, then block on basic_consume.
        Returns (or raises) when the connection is closed.
        """
        credentials = pika.PlainCredentials(
            username=self._cfg.get("username", "guest"),
            password=self._cfg.get("password", "guest"),
        )
        params = pika.ConnectionParameters(
            host=self._cfg.get("host",           "write-home-timeline-rabbitmq"),
            port=int(self._cfg.get("port",       5672)),
            credentials=credentials,
            heartbeat=60,
            blocked_connection_timeout=300,
        )

        logger.debug("Worker %d: connecting to RabbitMQ %s:%s",
                     worker_id, params.host, params.port)
        connection = pika.BlockingConnection(params)
        channel    = connection.channel()

        # Declare queue — idempotent, matches C++ AMQP-CPP declaration
        channel.queue_declare(
            queue=self._queue,
            durable=True,
            arguments={
                "x-message-ttl": 30_000,    # 30 s message TTL, matches C++
            },
        )

        # QoS: one unacked message per worker thread — mirrors C++ prefetch
        prefetch = int(self._cfg.get("prefetch_count", 1))
        channel.basic_qos(prefetch_count=prefetch)

        # Register callback
        channel.basic_consume(
            queue=self._queue,
            on_message_callback=self._make_callback(worker_id),
            auto_ack=False,   # manual ACK — same as C++
        )

        logger.info("Worker %d: consuming from queue=%s", worker_id, self._queue)

        try:
            # Blocks until connection is closed or stop_event fires
            while not self._stop_event.is_set():
                connection.process_data_events(time_limit=1)
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _make_callback(self, worker_id: int):
        """Return a pika on_message_callback bound to this worker_id."""

        def on_message(channel, method, properties, body):
            delivery_tag = method.delivery_tag
            try:
                self._worker.process(body)
                channel.basic_ack(delivery_tag=delivery_tag)
                logger.debug(
                    "Worker %d: ACK delivery_tag=%d", worker_id, delivery_tag
                )
            except Exception as exc:
                logger.warning(
                    "Worker %d: NACK delivery_tag=%d error=%s",
                    worker_id, delivery_tag, exc,
                )
                # NACK with requeue=True — message goes back to the queue
                # This matches the C++ error path.
                channel.basic_nack(
                    delivery_tag=delivery_tag, requeue=True
                )

        return on_message
