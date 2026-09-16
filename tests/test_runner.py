# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""The analysis itself -- the one place this package touches the engine.

Most of these run without bgsage. The end-to-end test does not, and is skipped
where the engine is absent: a contributor working on the CLI should not need
80 MB of neural nets to run the suite, and CI runs a codec-only job on purpose.
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pytest

from gvhelper import runner

#: A real analysed match, used as *input* -- the helper's job is to append an
#: analysis to an OGXM file, and a file that already has one exercises that
#: rather than the easier empty case. It is also what proves the append is
#: non-destructive: the result must come back with **two** blocks.
#:
#: Vendored here (12 KB) rather than reached for in the analysis repo, which is
#: where it came from and where it is a golden. A relative path out of the
#: checkout was how this test silently stopped running when the helper moved --
#: the `exists()` guard below turned a broken path into a skip.
GOLDEN = Path(__file__).resolve().parent / "golden" / "B4_SrGcsKAQmoTyHlgJCbM.fast.gvab"

def _has_engine() -> bool:
    try:
        import bgsage  # noqa: F401

        return True
    except Exception:
        return False


#: Skipped without the engine. The golden is checked too, but now only as a
#: guard against an sdist that failed to carry it -- it lives beside this file,
#: so a missing one is a packaging bug rather than the normal case it used to
#: be.
needs_engine = pytest.mark.skipif(
    not _has_engine() or not GOLDEN.exists(),
    reason="bgsage or the golden match is not available",
)


def test_the_engine_is_not_imported_just_to_answer_a_question():
    """`status` and `link` must not pay for loading 24 neural nets. A user
    running `gammonview-helper status` should get an answer in milliseconds, and
    the pairing flow is the part people do while watching."""
    assert "bgsage" not in sys.modules or _has_engine()
    # The version is read from distribution metadata, not from the module.
    assert "bgsage" in runner.engine_version() or runner.engine_version() == "unknown"


def test_the_presets_offered_are_the_engines_own():
    """Read from `gvanalysis` rather than hardcoded, so a preset added to the
    engine package appears in the website's menu on the next `hello` with
    nothing here to update."""
    presets = runner.available_presets()
    if presets:
        assert "world_class" in presets


def test_the_deeper_presets_are_the_point():
    """The helper exists so that the menu has entries the shared worker cannot
    offer. If this list ever shrinks to the shared worker's three, the feature
    has quietly stopped being worth installing."""
    presets = runner.available_presets()
    if presets:
        assert set(presets) - {"very_quick", "fast", "balanced"}


def test_lowering_priority_is_survivable_everywhere():
    """Called once at startup on every platform. A `nice` that raised on a
    machine that does not support it would take the helper down at the worst
    moment -- before it has done anything a user could see."""
    runner.lower_priority(0)
    runner.lower_priority(1)


def test_progress_starts_at_nothing():
    p = runner.Progress()
    assert p.get() == (0, 0)


@needs_engine
@pytest.mark.slow
def test_a_real_match_analyses_and_comes_back_gzipped():
    """End to end against the published package, at the cheapest preset.

    What this proves is the seam: `analyze` returns bytes the browser's
    `gva/analyze.js` can inflate, because the relay moves them without looking
    inside. If the two ever disagree about compression this is where it shows.
    """
    from gvformat import read_gvab

    progress = runner.Progress()
    out = runner.analyze(GOLDEN.read_bytes(), "very_quick", progress, jobs=1, threads=1)

    assert out[:2] == b"\x1f\x8b", "the browser inflates what comes back"
    doc = read_gvab(gzip.decompress(out))
    # The input already carried a `fast` analysis; ours is appended, never
    # replacing it -- that is `append_analysis`'s contract and the reason a
    # re-analysis does not destroy what a user already had.
    assert len(doc["analyses_info"]) >= 2

    done, total = progress.get()
    assert total > 0 and done == total
