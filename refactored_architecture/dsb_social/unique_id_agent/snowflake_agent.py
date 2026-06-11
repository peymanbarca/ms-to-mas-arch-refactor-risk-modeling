"""
snowflake_agent.py — SnowflakeGenerator extended for agent use.

The original SnowflakeGenerator.next_id() both advances the clock/sequence
AND assembles the final integer in one call. For the agent we need to
separate these two concerns:

  1. next_inputs()  — thread-safely advances the clock/sequence and returns
                      the raw triple (timestamp_ms, machine_id, sequence).
                      This is the deterministic, thread-safe part.

  2. The LLM graph  — receives the triple and reasons about the formula.

The original next_id() is kept intact for backwards compatibility and is
used by validate_output as the reference / fallback.
"""

import threading
import time

from .snowflake import (
    SnowflakeGenerator,
    _TIMESTAMP_SHIFT,
    _MACHINE_ID_SHIFT,
    _SEQUENCE_BITS,
    _MAX_SEQUENCE,
)


class AgentSnowflakeGenerator(SnowflakeGenerator):
    """
    Extends SnowflakeGenerator with next_inputs() which returns the raw
    (timestamp_ms, machine_id, sequence) triple without assembling the ID.

    The rest of the class (next_id, lock, machine_id, etc.) is unchanged.
    """

    def next_inputs(self) -> tuple[int, int, int]:
        """
        Thread-safely advance the clock/sequence counter and return
        (timestamp_ms, machine_id, sequence).

        This is the only method called by the LangGraph gather_inputs node.
        It is thread-safe (uses the same lock as next_id).
        """
        with self._lock:
            ms = self._current_ms()

            if ms < self._last_ms:
                raise RuntimeError(
                    f"Clock moved backwards: current={ms} last={self._last_ms}"
                )

            if ms == self._last_ms:
                self._sequence = (self._sequence + 1) & _MAX_SEQUENCE
                if self._sequence == 0:
                    while ms <= self._last_ms:
                        ms = self._current_ms()
            else:
                self._sequence = 0

            self._last_ms = ms
            return ms, self._machine_id, self._sequence
