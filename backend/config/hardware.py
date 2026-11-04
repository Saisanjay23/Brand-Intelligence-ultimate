"""What this machine has, in numbers the rest of the app can size itself by.

Standard library only, so `settings` can import it without a cycle and a
machine that never installed anything extra still gets the protection.

WHY THIS EXISTS. The backend process is small -- measured 2026-11-04, importing
the whole API costs ~135 MB. The memory is in the browsers, and one worker is
bigger than it looks (a real Chrome under patchright, persistent profile,
public profile pages):

    context open, no page      ~775 MB   (11 processes)
    one profile page           ~1.1 GB   (12 processes)
    three tabs                 ~1.6 GB   (14 processes)

A job sweeps every ready platform at once, so a handful of workers is several
GB before the analyst's own browser, editor and Mongo are counted. On a 16 GB
laptop that is the difference between a smooth run and a machine that starts
swapping -- at which point page loads time out and a good profile comes back
PARTIAL, a failure that looks like the platform's and is really ours.

TWO DEFENCES, BOTH ABOUT WHEN AND HOW MANY, NEITHER ABOUT WHAT:
  * `recommended_workers()` lowers the DEFAULT worker caps on small machines
    (never raises them, never overrides a value the operator set).
  * `wait_for_headroom()` lets a browser launch wait for free memory instead
    of piling onto a machine that is already full, then goes ahead anyway.

NOTHING HERE MAY BREAK A RUN. Every probe returns None when it cannot answer,
and None is never read as "no room": an unknown is never a reason to block.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
import time
from typing import Callable, Optional

# Measured per-worker cost, worst case (three tabs). See the module docstring.
BROWSER_WORKER_MB = 1600
# Share of physical RAM the browsers may plan on. The rest is the OS, Mongo,
# the API process and whatever else the analyst has open.
BROWSER_RAM_SHARE = 0.45


def _windows_memory() -> Optional[tuple[int, int]]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
        return None
    return int(st.ullTotalPhys // 1048576), int(st.ullAvailPhys // 1048576)


def _linux_memory() -> Optional[tuple[int, int]]:
    info: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                info[key] = int(rest.split()[0])
    if "MemTotal" not in info or "MemAvailable" not in info:
        return None
    return info["MemTotal"] // 1024, info["MemAvailable"] // 1024


def _darwin_memory() -> Optional[tuple[int, int]]:
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                               text=True, timeout=2).stdout.strip()) // 1048576
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
    page = 4096
    pages: dict[str, int] = {}
    for line in out.splitlines():
        if "page size of" in line:
            page = int(line.split("page size of")[1].split()[0])
        key, _, rest = line.partition(":")
        if key.startswith("Pages "):
            pages[key] = int(rest.strip().rstrip("."))
    free = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative"))
    return total, free * page // 1048576


def memory_mb() -> Optional[tuple[int, int]]:
    """(total, available) physical memory in MB, or None when this platform
    cannot say. "Available" counts memory the OS can hand back at once
    (cache included), which is what a new process can actually get."""
    try:
        if sys.platform.startswith("win"):
            return _windows_memory()
        if sys.platform == "darwin":
            return _darwin_memory()
        if sys.platform.startswith("linux"):
            return _linux_memory()
    except Exception:  # noqa: BLE001 - a probe must never be the thing that fails
        return None
    return None


def total_ram_mb() -> Optional[int]:
    got = memory_mb()
    return got[0] if got else None


def available_ram_mb() -> Optional[int]:
    got = memory_mb()
    return got[1] if got else None


def logical_cores() -> int:
    """Logical CPUs this process may actually use (honours a CPU affinity
    mask where the OS has one), never less than 1."""
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def recommended_workers(total_mb: Optional[int] = None, cores: Optional[int] = None) -> int:
    """How many browser workers this machine can run without swapping.

    The smaller of a memory bound and a CPU bound, never below 1:
      memory  45% of RAM / the measured worst-case worker
      cpu     half the logical cores -- a Chrome rendering a heavy single-page
              app wants a core of its own, and more workers than that only
              make every page slower until it times out

    For a 16 GB / 4-core-8-thread laptop this is 4, the old default; it only
    bites below that. Pure arithmetic, so it is testable without the machine.
    """
    total = total_ram_mb() if total_mb is None else total_mb
    n_cores = logical_cores() if cores is None else cores
    by_cpu = max(1, n_cores // 2)
    if total is None:
        return by_cpu
    by_ram = max(1, int(total * BROWSER_RAM_SHARE // BROWSER_WORKER_MB))
    return max(1, min(by_ram, by_cpu))


async def wait_for_headroom(
    need_mb: int,
    max_wait_s: float,
    *,
    stop: Callable[[], bool] = lambda: False,
    poll_s: float = 2.0,
    probe: Callable[[], Optional[int]] = available_ram_mb,
    sleep=asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, Optional[int]]:
    """Wait (up to `max_wait_s`) until `need_mb` is free.

    -> (enough, last_reading). `enough` is True when there is room OR the
    machine could not be read; False only when the wait ran out or `stop()`
    fired. It never raises and never waits longer than asked, so the caller
    can always go ahead afterwards -- this delays a launch, it never refuses
    one. The probe, sleep and clock are injectable so the timing is testable
    without waiting.
    """
    avail = probe()
    if avail is None or avail >= need_mb:
        return True, avail
    deadline = clock() + max_wait_s
    while True:
        if stop():
            return False, avail
        remaining = deadline - clock()
        if remaining <= 0:
            return False, avail
        await sleep(min(poll_s, remaining))
        avail = probe()
        if avail is None or avail >= need_mb:
            return True, avail
