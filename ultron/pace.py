"""Pacing: a minimum pause between executor calls across all ultron processes.

The stamp of the last call lives in the state root, so parallel sessions
share it. A broken or missing stamp never blocks a call.
"""

from __future__ import annotations

import os
import pathlib
import time
from typing import Callable

from ultron import policy

STAMP_NAME = "last-call"


def pace_seconds() -> float:
    """Pause from the environment override or policy; never negative."""
    raw = os.environ.get(policy.PACE_ENV)
    if raw is None:
        return float(policy.PACE_SECONDS)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(policy.PACE_SECONDS)


def wait_turn(
    root: pathlib.Path,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Sleep until the pause since the last call has passed; return seconds slept."""
    pause = pace_seconds()
    if pause <= 0:
        return 0.0
    stamp = root / STAMP_NAME
    waited = 0.0
    try:
        last = float(stamp.read_text(encoding="utf-8").strip())
        remaining = pause - (now() - last)
        if 0 < remaining <= pause:
            sleep(remaining)
            waited = remaining
    except (OSError, ValueError):
        pass
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now()), encoding="utf-8")
    except OSError:
        pass
    return waited
