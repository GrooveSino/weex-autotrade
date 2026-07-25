"""Small, dependency-free process metrics for the Fleet capacity endpoint."""

from __future__ import annotations

import os
import resource
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutorProcessSnapshot:
    open_file_descriptors: int
    rss_bytes: int


def process_snapshot() -> ExecutorProcessSnapshot:
    return ExecutorProcessSnapshot(_open_file_descriptors(), _rss_bytes())


def _open_file_descriptors() -> int:
    fd_directory = Path("/dev/fd")
    try:
        return len(tuple(fd_directory.iterdir()))
    except OSError:
        return 0


def _rss_bytes() -> int:
    proc_statm = Path("/proc/self/statm")
    try:
        resident_pages = int(proc_statm.read_text(encoding="ascii").split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (IndexError, OSError, ValueError):
        pass
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Darwin reports bytes while Linux reports kibibytes for ru_maxrss.
    return value if sys.platform == "darwin" else value * 1_024
