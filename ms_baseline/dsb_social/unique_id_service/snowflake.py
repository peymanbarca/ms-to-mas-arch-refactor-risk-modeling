"""
Snowflake ID generator — ported from DeathStarBench C++ UniqueIdHandler.h

Bit layout (64-bit signed integer, matching the original):
  [63]       always 0  (positive sentinel)
  [62..22]   41-bit timestamp in milliseconds since custom epoch
  [21..12]   10-bit machine_id  (0..1023)
  [11..0]    12-bit sequence counter per millisecond

The C++ original uses:
  - std::chrono::system_clock for timestamp
  - A machine_id read from service-config.json
  - A per-instance atomic counter reset each millisecond
  - A custom epoch of 0 (Unix ms) — the counter just uses raw ms since epoch
"""

import threading
import time


# Bit widths — must match the C++ constants in UniqueIdHandler.h
_TIMESTAMP_BITS = 41
_MACHINE_ID_BITS = 10
_SEQUENCE_BITS   = 12

_MAX_MACHINE_ID = (1 << _MACHINE_ID_BITS) - 1   # 1023
_MAX_SEQUENCE   = (1 << _SEQUENCE_BITS) - 1      # 4095

_MACHINE_ID_SHIFT  = _SEQUENCE_BITS                          # 12
_TIMESTAMP_SHIFT   = _SEQUENCE_BITS + _MACHINE_ID_BITS       # 22


class SnowflakeGenerator:
    """
    Thread-safe Snowflake ID generator.

    Faithfully replicates the C++ UniqueIdHandler behaviour:
    - Same bit layout
    - Blocks (spin-waits) when the sequence overflows within a millisecond,
      identical to the C++ implementation using std::this_thread::yield()
    - machine_id comes from service-config.json (passed in at construction)
    """

    def __init__(self, machine_id: int):
        if not (0 <= machine_id <= _MAX_MACHINE_ID):
            raise ValueError(
                f"machine_id must be in [0, {_MAX_MACHINE_ID}], got {machine_id}"
            )
        self._machine_id = machine_id
        self._sequence   = 0
        self._last_ms    = -1
        self._lock       = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def next_id(self) -> int:
        """Return a unique 64-bit Snowflake ID (always positive)."""
        with self._lock:
            return self._generate()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _current_ms(self) -> int:
        return int(time.time() * 1000)

    def _generate(self) -> int:
        ms = self._current_ms()

        if ms < self._last_ms:
            # Clock moved backwards — same mitigation as the C++ code:
            # raise a ServiceException rather than silently producing a
            # duplicate.  Callers should propagate this as SE_THRIFT_HANDLER_ERROR.
            raise RuntimeError(
                f"Clock moved backwards: current={ms} last={self._last_ms}"
            )

        if ms == self._last_ms:
            self._sequence = (self._sequence + 1) & _MAX_SEQUENCE
            if self._sequence == 0:
                # Sequence overflow in this millisecond — spin until the
                # next millisecond (mirrors std::this_thread::yield() loop).
                while ms <= self._last_ms:
                    ms = self._current_ms()
        else:
            self._sequence = 0

        self._last_ms = ms

        snowflake = (
            (ms             << _TIMESTAMP_SHIFT)
            | (self._machine_id << _MACHINE_ID_SHIFT)
            | self._sequence
        )
        return snowflake
