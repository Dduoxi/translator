"""Make sure the game always wins the CPU fight.

Two deliberately blunt instruments:

* **Priority** -- drop below normal. When the game wants a core, the OS
  preempts us instantly and our OCR just takes longer. This is the single most
  effective guarantee we can give, and it costs nothing when the game is idle.
* **Affinity** (opt-in) -- keep our threads off the cores the game uses. Most
  games saturate the first N logical cores, so pinning ourselves to the last
  ``cpu_reserve`` of them avoids cache thrash. Off by default: over-constraining
  can make latency bad enough to be useless on small CPUs.

Both are best-effort: failing is fine, we just run at normal priority.
"""

from __future__ import annotations

import logging
import sys

__all__ = ["apply_resource_policy", "lower_priority", "reserve_cores"]

log = logging.getLogger(__name__)


def lower_priority() -> str:
    try:
        import psutil

        proc = psutil.Process()
        if sys.platform == "win32":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return "windows:BELOW_NORMAL"
        current = proc.nice()
        if current < 10:
            proc.nice(10)
        return f"nice:{proc.nice()}"
    except Exception as exc:  # noqa: BLE001
        log.debug("could not lower priority: %s", exc)
        return "unchanged"


def reserve_cores(count: int) -> str:
    """Restrict this process to the last ``count`` logical cores (best effort)."""
    if count <= 0:
        return "disabled"
    try:
        import psutil

        proc = psutil.Process()
        total = psutil.cpu_count(logical=True) or 1
        if count >= total:
            return "skipped (too few cores)"
        # Pin to the *last* cores: games start their threads on the first ones.
        allowed = list(range(total - count, total))
        proc.cpu_affinity(allowed)
        return f"cpus={allowed}"
    except Exception as exc:  # noqa: BLE001
        log.debug("could not set affinity: %s", exc)
        return "unchanged"


def apply_resource_policy(*, low_priority: bool = True, cpu_reserve: int = 0) -> list[str]:
    notes: list[str] = []
    if low_priority:
        notes.append(f"priority={lower_priority()}")
    if cpu_reserve:
        notes.append(f"affinity={reserve_cores(cpu_reserve)}")
    if notes:
        log.info("resource policy: %s", ", ".join(notes))
    return notes
