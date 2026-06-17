"""
consumer.py — RabbitMQ consumer for WriteHomeTimelineService.

This version uses the worker's graph-backed decision:
- approved=True   -> ACK
- approved=False  -> ACK as intentional reject (prevents poison/requeue loops)
- exception       -> NACK requeue=True
"""

import logging
import threading
import time

import pika
import pika.exceptions

from .worker import MessageWorker

logger = logging.getLogger("write-home-timeline-service.consumer")

_RECONNECT_INITIAL_DELAY = 1.0
_RECONNECT_MAX_DELAY = 30.0
_RECONNECT_MULTIPLIER = 2.0


class WriteHomeTimelineConsumer:
    """
    Multi-threaded RabbitMQ consumer for the write-home-timeline queue.
    """

    def __init__(
        self,
        rabbitmq_cfg: dict,
        queue_name: str,
        worker: MessageWorker,
        num_workers: int = 4,
    ):
        self._cfg = rabbitmq_cfg
        self._queue = queue_name
        self._worker = worker
        self._num_workers = num_workers
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
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
        logger.info("Stopping WriteHomeTimelineConsumer...")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=10)

    def join(self) -> None:
        for t in self._threads:
            t.join()

    def _run_worker_loop(self, worker_id: int) -> None:
        delay = _RECONNECT_INITIAL_DELAY
        while not self._stop_event.is_set():
            try:
                self._consume_blocking(worker_id)
                delay = _RECONNECT_INITIAL_DELAY
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
        credentials = pika.PlainCredentials(
            username=self._cfg.get("username", "guest"),
            password=self._cfg.get("password", "guest"),
        )
        params = pika.ConnectionParameters(
            host=self._cfg.get("host", "write-home-timeline-rabbitmq"),
            port=int(self._cfg.get("port", 5672)),
            credentials=credentials,
            heartbeat=60,
            blocked_connection_timeout=300,
        )

        logger.debug(
            "Worker %d: connecting to RabbitMQ %s:%s",
            worker_id, params.host, params.port,
        )
        connection = pika.BlockingConnection(params)
        channel = connection.channel()

        channel.queue_declare(
            queue=self._queue,
            durable=True,
            arguments={
                "x-message-ttl": 30_000,
            },
        )

        prefetch = int(self._cfg.get("prefetch_count", 1))
        channel.basic_qos(prefetch_count=prefetch)

        channel.basic_consume(
            queue=self._queue,
            on_message_callback=self._make_callback(worker_id),
            auto_ack=False,
        )

        logger.info("Worker %d: consuming from queue=%s", worker_id, self._queue)

        try:
            while not self._stop_event.is_set():
                connection.process_data_events(time_limit=1)
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _make_callback(self, worker_id: int):
        def on_message(channel, method, properties, body):
            delivery_tag = method.delivery_tag
            try:
                approved = self._worker.process(body)

                # Intentional reject is NOT a runtime failure.
                # ACK it so it doesn't poison the queue forever.
                channel.basic_ack(delivery_tag=delivery_tag)

                if approved:
                    logger.debug(
                        "Worker %d: ACK delivery_tag=%d approved=True",
                        worker_id, delivery_tag,
                    )
                else:
                    logger.debug(
                        "Worker %d: ACK delivery_tag=%d approved=False (rejected)",
                        worker_id, delivery_tag,
                    )

            except Exception as exc:
                logger.warning(
                    "Worker %d: NACK delivery_tag=%d error=%s",
                    worker_id, delivery_tag, exc,
                )
                channel.basic_nack(delivery_tag=delivery_tag, requeue=True)

        return on_message