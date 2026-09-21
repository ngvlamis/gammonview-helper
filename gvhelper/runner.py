# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Run one job: OGXM binary in, analysed gzipped `.gvab` out.

The same work `server/gvserver/jobs.py` does for the shared worker, and
deliberately the same call -- `gvanalysis.mat_to_gvab` -- so a match analysed
here and a match analysed there differ in nothing but which CPU ran it.

**Where this departs from the shared worker, and why.** gvserver runs each job
in a `ProcessPoolExecutor` slot because it serves many users at once and needs
to bound, queue and abandon them independently. The helper serves one person and
runs one job at a time, so that machinery would buy nothing -- and it would buy
it at the price of nesting one process pool inside another, since `gvanalysis`
already parallelises *within* a job.

So the analysis runs on a thread here, and the isolation people usually want a
subprocess for is already present one level down: with `jobs` anything but 1
the engine work happens in `gvanalysis`'s own worker processes, and an engine
that dies takes one of those with it and surfaces as `BrokenProcessPool` -- an
exception this module catches and reports, rather than a helper that vanishes.

**Both parallelism axes default to 0, which means "size it yourself".** The
library's own default is `jobs=1`, serial, and that is a deliberate safety
default for callers who may not have guarded `if __name__ == "__main__":` --
spawning without that guard fails as a bare `BrokenProcessPool` naming nothing
relevant. A console script owns its entry point, so the caveat does not apply
here and the default is simply expensive: gvanalysis measured serial at 44% off
the pace on 24 cores. `config.py` says why the helper no longer picks the
numbers itself.
"""

from __future__ import annotations

import gzip
import os
import sys
import tempfile
import threading
from pathlib import Path


def lower_priority(nice: int) -> None:
    """Get out of the way of whatever the user is actually doing.

    This is the line `docs/DesktopHelper.md` insists must survive: a background
    analyser that makes someone's laptop feel slow gets uninstalled, and that is
    the way this feature dies. Niceness costs nothing when the machine is idle
    -- which is most of the time -- and costs the analysis some wall clock
    exactly when the user would otherwise have noticed it.

    Called once in the helper process; `gvanalysis`'s worker processes inherit
    it, which is why there is no per-worker initializer to keep in step.
    """
    if nice <= 0:
        return
    if hasattr(os, "nice"):
        try:
            os.nice(nice)
        except OSError:  # pragma: no cover - already niced, or not permitted
            pass
        return
    if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
        # Windows has no nice(2). `BELOW_NORMAL_PRIORITY_CLASS` is the nearest
        # thing and is what a well-behaved background task uses; done with
        # ctypes rather than psutil so this costs no dependency.
        try:
            import ctypes

            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(
                kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS
            )
        except Exception:
            pass


class Progress:
    """The counts the analyser has reached, readable from another thread.

    A lock rather than bare attributes because the two numbers are read as a
    pair and posted as a pair; a torn read would put a `done` from one decision
    beside a `total` from before the total was known, and the browser would draw
    a bar that jumps backwards.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = 0
        self._total = 0

    def set(self, done: int, total: int) -> None:
        with self._lock:
            self._done, self._total = int(done), int(total)

    def get(self) -> tuple[int, int]:
        with self._lock:
            return self._done, self._total


def analyze(
    match_bytes: bytes,
    preset: str,
    progress: Progress,
    *,
    jobs: int = 0,
    threads: int = 0,
) -> bytes:
    """Analyse OGXM binary and return the gzipped analysed `.gvab`.

    Gzipped because that is what the browser's `gva/analyze.js` already inflates
    coming back from the shared worker -- the relay moves bytes and does not look
    inside them, so the two paths have to agree here rather than at the relay.

    `mat_to_gvab` dispatches on the file extension, so the temp file must keep
    its `.gvab` suffix. It is removed in a `finally`: a helper runs for weeks on
    somebody's laptop, and a leaked temp file per analysis is the kind of thing
    that is noticed as "this program fills my disk".
    """
    fd, path = tempfile.mkstemp(suffix=".gvab")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(match_bytes)
        return gzip.compress(
            mat_to_gvab(
                path,
                preset=preset or None,
                on_progress=progress.set,
                jobs=jobs,
                threads=threads,
                quiet=True,
                show_progress=False,
            )
        )
    finally:
        try:
            Path(path).unlink()
        except OSError:  # pragma: no cover
            pass


def mat_to_gvab(*args, **kwargs):
    """Indirection so the import cost lands on the first job, not on `--help`.

    `gvanalysis` pulls in bgsage, which loads 24 neural nets and a bearoff
    database -- around a second, and a large chunk of the helper's resident
    memory. A user running `gammonview-helper status` should not pay for that,
    and neither should the pairing flow, which is the part people do while
    watching.
    """
    from gvanalysis import mat_to_gvab as _impl

    return _impl(*args, **kwargs)


def engine_version() -> str:
    """The bgsage version this helper would analyse with, for `hello`.

    Read from distribution metadata rather than from the module, because bgsage
    exports no `__version__` -- and asked without importing bgsage, so this stays
    cheap enough to call at startup.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"bgsage {version('bgsage')}"
    except PackageNotFoundError:  # pragma: no cover - installed without [engine]
        return "unknown"


def available_presets() -> list[str]:
    """Every preset this machine will accept.

    The whole point of the helper is that this list is longer than the shared
    worker's three -- `world_class` and `deep` are here because nobody else is
    waiting in a queue behind them. Read from `gvanalysis` rather than hardcoded
    so that a preset added to the engine package appears in the site's menu on
    the next `hello`, with nothing here to update.

    In the engine's own key order, which is roughly ascending cost, rather than
    sorted. `sorted()` threw away information for no gain: it made the site's
    menu read *Deep, Fast, Quick, World Class, World Class Fast*, whose first
    button is the third-cheapest thing on it. The site orders what it receives
    anyway -- it has to, because a helper installed before this change goes on
    reporting the old order for as long as its owner leaves it alone -- so this
    is about `status` and the relay row reading sensibly, not about the menu
    being correct.
    """
    try:
        from gvanalysis.presets import PRESETS

        return list(PRESETS)
    except Exception:  # pragma: no cover - installed without [engine]
        return []
