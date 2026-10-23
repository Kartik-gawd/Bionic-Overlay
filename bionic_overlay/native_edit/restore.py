"""Formatting snapshots for safe restore.

Idea: before emphasizing, record the ORIGINAL bold state of the target as
run-length-encoded runs (start, end, bold). Capture uses recursive bisection
over the host's "get bold for range" call, which returns True/False/None(mixed):
a uniformly formatted range costs ONE COM call, a mixed one costs O(runs*log n).
Restore re-applies exactly those runs, touching only the bold property, so
every unrelated attribute (font, size, colour, italics, ...) is never read or written.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

Run = tuple[int, int, bool]
GetBold = Callable[[int, int], Optional[bool]]
SetBold = Callable[[int, int, bool], None]


class SnapshotError(Exception):
    """The original formatting cannot be captured reliably."""


class SnapshotTooComplex(SnapshotError):
    """Capturing would exceed the configured COM-call budget."""


def merge_runs(runs: list[Run]) -> list[Run]:
    merged: list[Run] = []
    for a, b, s in sorted(runs):
        if merged and merged[-1][2] == s and merged[-1][1] == a:
            merged[-1] = (merged[-1][0], b, s)
        else:
            merged.append((a, b, s))
    return merged


def capture_runs(length: int, get_bold: GetBold, max_calls: int) -> list[Run]:
    calls = 0
    out: list[Run] = []

    def rec(a: int, b: int) -> None:
        nonlocal calls
        calls += 1
        if calls > max_calls:
            raise SnapshotTooComplex(f"more than {max_calls} calls needed to snapshot formatting")
        state = get_bold(a, b)
        if state is not None:
            out.append((a, b, state))
            return
        if b - a <= 1:
            raise SnapshotError("bold state of a single character is indeterminate")
        mid = (a + b) // 2
        rec(a, mid)
        rec(mid, b)

    if length > 0:
        rec(0, length)
    return merge_runs(out)


def overlay(runs: list[Run], spans: list[tuple[int, int]], value: bool = True) -> list[Run]:
    """Return `runs` with every span forced to `value` (computed offline, no host calls)."""
    if not runs:
        return []
    end = runs[-1][1]
    cuts = {0, end}
    for a, b, _ in runs:
        cuts.update((a, b))
    for a, b in spans:
        cuts.update((max(0, min(a, end)), max(0, min(b, end))))
    points = sorted(cuts)
    result: list[Run] = []
    ri, si = 0, 0
    sp = sorted(spans)
    for a, b in zip(points, points[1:]):
        while runs[ri][1] <= a:
            ri += 1
        while si < len(sp) and sp[si][1] <= a:
            si += 1
        in_span = si < len(sp) and sp[si][0] <= a < sp[si][1]
        result.append((a, b, value if in_span else runs[ri][2]))
    return merge_runs(result)


def restore_runs(runs: list[Run], set_bold: SetBold) -> None:
    for a, b, state in runs:
        set_bold(a, b, state)


def runs_match_sample(runs: list[Run], get_bold: GetBold, max_samples: int = 64) -> bool:
    """Cheap check that the live formatting still equals `runs` (sampled, not exhaustive)."""
    if not runs:
        return True
    step = max(1, len(runs) // max_samples)
    for a, b, expected in runs[::step]:
        got = get_bold(a, b)
        if got is not None and got != expected:
            return False
        if got is None:  # mixed where we expected uniform => someone edited formatting
            return False
    return True


@dataclass
class TargetSnapshot:
    index: int                 # position of the target in the SelectionContext
    before: list[Run]
    applied: list[Run]


@dataclass
class Snapshot:
    entries: list[TargetSnapshot] = field(default_factory=list)


class SnapshotStore:
    """In-memory LRU. Keyed by a digest of (selection identity + exact text)."""

    def __init__(self, max_entries: int = 32) -> None:
        self._data: OrderedDict[str, Snapshot] = OrderedDict()
        self._max = max_entries

    def put(self, key: str, snap: Snapshot) -> None:
        self._data[key] = snap
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def get(self, key: str) -> Snapshot | None:
        return self._data.get(key)

    def pop(self, key: str) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)
