from __future__ import annotations

from pathlib import Path

import pytest

from kalshi_maker.instance_lock import AlreadyRunning, InstanceLock


def test_second_instance_refused_and_lock_released(tmp_path: Path) -> None:
    p = tmp_path / "run.lock"
    with InstanceLock(p), pytest.raises(AlreadyRunning):
        InstanceLock(p).acquire()
    # released on exit: a restart can take it again
    with InstanceLock(p):
        pass
