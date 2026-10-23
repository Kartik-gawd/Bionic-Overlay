"""Hotkey orchestration: detect selection -> plan -> verify -> apply (or restore).

All safety policy lives here, independent of any host application:
  * unsupported app / no selection / uncertain state  => NOTHING is touched;
  * every write is preceded by a read-only planning phase for ALL targets;
  * a failure mid-write rolls back what was already written;
  * the clipboard is never read or written (no clipboard code exists in this package).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .spans import BionicEngine, to_utf16_spans, utf16_len
from .config import AppConfig
from .restore import (Run, Snapshot, SnapshotError, SnapshotStore, TargetSnapshot,
                      capture_runs, overlay, restore_runs, runs_match_sample)
from .selection import (ForegroundInfo, SelectionContext, SelectionDispatcher,
                        SelectionUncertain, TextTarget, get_foreground_info)

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    APPLIED = "applied"
    RESTORED = "restored"
    NO_SELECTION = "no_selection"
    UNSUPPORTED_APP = "unsupported_app"
    UNCERTAIN_NOT_MODIFIED = "uncertain_not_modified"
    NOTHING_TO_CHANGE = "nothing_to_change"
    NOTHING_TO_RESTORE = "nothing_to_restore"
    ERROR = "error"


@dataclass
class _Plan:
    index: int
    target: TextTarget
    spans16: list[tuple[int, int]]
    before: Optional[list[Run]]
    length16: int


class NativeEditController:
    def __init__(self, cfg: AppConfig, dispatcher: SelectionDispatcher, *,
                 foreground: Callable[[], Optional[ForegroundInfo]] = get_foreground_info,
                 store: Optional[SnapshotStore] = None,
                 engine: Optional[BionicEngine] = None) -> None:
        self.cfg = cfg
        self.dispatcher = dispatcher
        self.foreground = foreground
        self.store = store or SnapshotStore()
        self.engine = engine or BionicEngine(cfg.bionic)

    # ------------------------------------------------------------------ entry
    def handle(self, mode: str = "toggle") -> Outcome:
        """mode: 'toggle' (apply, or restore if already applied), 'apply', 'restore'."""
        try:
            info = self.foreground()
            if info is None:
                log.debug("No foreground window information; ignoring hotkey")
                return Outcome.UNSUPPORTED_APP
            provider = self.dispatcher.provider_for(info)
            if provider is None:
                log.debug("Unsupported application %s; ignoring hotkey", info.exe)
                return Outcome.UNSUPPORTED_APP
            try:
                ctx = provider.acquire(info)
            except SelectionUncertain as exc:
                log.warning("%s: selection uncertain, document NOT modified: %s", provider.name, exc)
                return Outcome.UNCERTAIN_NOT_MODIFIED
            if ctx is None:
                log.info("%s: no text selection; nothing to do", provider.name)
                return Outcome.NO_SELECTION
            return self._run(ctx, mode)
        except Exception:  # never let a hotkey callback kill the daemon
            log.exception("Unexpected failure while handling hotkey; document state unknown")
            return Outcome.ERROR

    # ---------------------------------------------------------------- internals
    def _run(self, ctx: SelectionContext, mode: str) -> Outcome:
        texts = [t.text() for t in ctx.targets]
        total = sum(len(t) for t in texts)
        if total == 0:
            return Outcome.NO_SELECTION
        if total > self.cfg.office.max_selection_chars:
            log.warning("Selection of %d chars exceeds max_selection_chars=%d; NOT modified",
                        total, self.cfg.office.max_selection_chars)
            return Outcome.UNCERTAIN_NOT_MODIFIED

        digest = hashlib.sha256(("\x00".join([ctx.key, *texts])).encode("utf-8", "surrogatepass")).hexdigest()
        snap = self.store.get(digest) if mode in ("toggle", "restore") else None

        if snap is not None:
            if self._still_applied(ctx, snap):
                with ctx.undo_scope():
                    for e in snap.entries:
                        restore_runs(e.before, ctx.targets[e.index].set_bold)
                self.store.pop(digest)
                log.info("%s: restored original formatting (%d target(s))", ctx.app, len(snap.entries))
                return Outcome.RESTORED
            log.info("Formatting changed since Bionic was applied; discarding stale snapshot")
            self.store.pop(digest)
        if mode == "restore":
            log.info("No restorable Bionic snapshot for this selection")
            return Outcome.NOTHING_TO_RESTORE

        plans = self._plan(ctx, texts)
        if plans is None:
            return Outcome.UNCERTAIN_NOT_MODIFIED
        if not plans:
            log.info("%s: nothing to emphasize in this selection", ctx.app)
            return Outcome.NOTHING_TO_CHANGE
        return self._apply(ctx, plans, digest)

    def _still_applied(self, ctx: SelectionContext, snap: Snapshot) -> bool:
        try:
            return all(runs_match_sample(e.applied, ctx.targets[e.index].get_bold) for e in snap.entries)
        except Exception:
            log.exception("Could not compare live formatting with snapshot")
            return False

    def _plan(self, ctx: SelectionContext, texts: list[str]) -> Optional[list[_Plan]]:
        """READ-ONLY phase. Returns None if nothing may be modified (uncertain)."""
        ocfg = self.cfg.office
        plans: list[_Plan] = []
        skipped = 0
        for i, (target, text) in enumerate(zip(ctx.targets, texts)):
            problem = target.editable_problem()
            if problem:
                log.warning("Skipping %s: %s", target.label or i, problem)
                skipped += 1
                continue
            spans = self.engine.spans(text)
            if not spans:
                continue
            spans16 = to_utf16_spans(text, spans)
            if not self._verify_mapping(target, text, spans, spans16, ocfg.verify_sample_every):
                log.warning("Skipping %s: host text does not match expected offsets", target.label or i)
                skipped += 1
                continue
            length16 = utf16_len(text)
            try:
                before = capture_runs(length16, target.get_bold, ocfg.max_snapshot_calls)
            except SnapshotError as exc:
                if ocfg.require_restorable:
                    log.warning("Skipping %s: cannot snapshot original formatting (%s)", target.label or i, exc)
                    skipped += 1
                    continue
                before = None
            if before is not None and overlay(before, spans16) == before:
                continue  # everything we would embolden already is bold
            plans.append(_Plan(i, target, spans16, before, length16))
        if not plans and skipped:
            return None  # we wanted to act but every target was unsafe
        return plans

    @staticmethod
    def _verify_mapping(target: TextTarget, text: str, spans: list[tuple[int, int]],
                        spans16: list[tuple[int, int]], every: int) -> bool:
        idx = {0, len(spans) - 1, *range(0, len(spans), max(1, every))}
        try:
            return all(target.verify(*spans16[k], text[spans[k][0]:spans[k][1]]) for k in sorted(idx))
        except Exception:
            log.exception("Mapping verification raised")
            return False

    def _apply(self, ctx: SelectionContext, plans: list[_Plan], digest: str) -> Outcome:
        done: list[_Plan] = []
        current: Optional[_Plan] = None
        try:
            with ctx.undo_scope():
                for plan in plans:
                    current = plan
                    for a, b in plan.spans16:
                        plan.target.set_bold(a, b, True)
                    done.append(plan)
                    current = None
        except Exception:
            log.exception("Write failed midway; attempting rollback")
            for plan in done + ([current] if current else []):
                if plan.before is None:
                    log.error("Cannot roll back %s: no snapshot", plan.target.label)
                    continue
                try:
                    restore_runs(plan.before, plan.target.set_bold)
                except Exception:
                    log.exception("Rollback of %s failed", plan.target.label)
            return Outcome.ERROR

        entries = [TargetSnapshot(p.index, p.before, overlay(p.before, p.spans16))
                   for p in plans if p.before is not None]
        if entries:
            self.store.put(digest, Snapshot(entries))
        words = sum(len(p.spans16) for p in plans)
        log.info("%s: emphasized %d word(s) in %d target(s)%s", ctx.app, words, len(plans),
                 "" if entries else " (NOT restorable)")
        return Outcome.APPLIED
