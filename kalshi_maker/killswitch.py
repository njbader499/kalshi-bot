"""Kill switch (Section 7): Telegram /kill, a local KILL file, or SIGTERM.

All three converge on the same path. A KILL file is written (so the supervisor doesn't
restart the bot, and a manual start refuses until it's deleted), the main loop sees it
within a second, cancels every bot order and exits with code 10. In live mode the
RiskManager is also KILLED, persisted in risk_state.json, so deleting the file alone is not
enough: `risk-reset --confirm` is required.

Signals: SIGTERM everywhere; on Windows also SIGBREAK (Ctrl+Break). Ctrl+C (SIGINT) is a
normal graceful stop that still cancels bot orders on shutdown, but writes no KILL file.
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def write_kill_file(path: Path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {reason}\n")


def install_signal_kill(kill_file: Path, on_kill: Callable[[str], None] | None = None) -> list[int]:
    """Route SIGTERM (and SIGBREAK on Windows) to the kill path. Returns signals hooked."""
    hooked: list[int] = []

    def handler(signum: int, _frame: object) -> None:
        reason = f"signal {signal.Signals(signum).name}"
        write_kill_file(kill_file, reason)
        log.error("kill switch", extra={"reason": reason})
        if on_kill is not None:
            on_kill(reason)

    sigs = [signal.SIGTERM]
    if sys.platform == "win32":
        sigs.append(signal.SIGBREAK)
    for s in sigs:
        signal.signal(s, handler)
        hooked.append(int(s))
    return hooked
