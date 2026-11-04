"""Keep the machine awake while the bot runs.

Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) on the calling
thread, cleared on exit. This stops *idle* sleep only. Closing the lid, pressing the power
button or choosing Sleep still suspends the laptop; the continuity monitor
(continuity.py) catches that on wake, and exchange-side order expiry covers the gap.

Other OSes: no-op (a VPS doesn't sleep; Phase 9 targets Linux).
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_state(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
    return bool(kernel32.SetThreadExecutionState(flags))


@contextmanager
def keep_awake(enabled: bool = True) -> Iterator[bool]:
    """Context manager. Yields True if sleep prevention is active.

    The execution state belongs to the thread that set it, so enter and exit on the same
    thread (the main thread running the asyncio loop).
    """
    active = False
    owner = threading.get_ident()
    if enabled:
        active = _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if active:
            log.info("sleep prevention on (SetThreadExecutionState)")
        elif sys.platform == "win32":
            log.warning("SetThreadExecutionState failed; the laptop may sleep while running")
    try:
        yield active
    finally:
        if active:
            if threading.get_ident() != owner:
                log.warning("keep_awake exited on a different thread; state may persist")
            _set_state(ES_CONTINUOUS)
            log.info("sleep prevention off")
