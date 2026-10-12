"""One run: for each due week, fill every platform, confirm, submit, then archive the week."""

from __future__ import annotations

import logging
import textwrap
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .platforms.base import Platform, PlatformError
from .state import SAVED, SUBMITTED, State
from .timesheet import FileLockedError, TimesheetFile, Week

log = logging.getLogger("lazy_bot")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
Confirm = Callable[[str, Callable[[], None]], bool]


@dataclass
class Line:
    """One platform's row in the summary."""

    platform: Platform
    status: str = ""
    pending: str = ""  # what answering "y" will do; empty when nothing
    failed: bool = False


class Runner:
    def __init__(
        self,
        settings: Settings,
        state: State,
        timesheet: TimesheetFile,
        platforms: list[Platform],
        *,
        mode: str,
        run_dir: Path,
        only: set[str] | None = None,
        force: bool = False,
        open_session: Callable[[Platform], object] | None = None,
        confirm: Confirm = lambda question, pump: False,
        retry_locked: Callable[[str], bool] = lambda message: False,
    ):
        self.settings = settings
        self.state = state
        self.timesheet = timesheet
        self.platforms = platforms  # every enabled platform; `only` limits which ones this run touches
        self.mode = mode
        self.run_dir = run_dir
        self.only = only
        self.force = force
        self.open_session = open_session
        self.confirm = confirm
        self.retry_locked = retry_locked
        self._signed_in: set[str] = set()

    def run(self, weeks: list[Week]) -> bool:
        """True when nothing failed."""
        ok = True
        for week in weeks:
            ok = self.run_week(week) and ok
        return ok

    def run_week(self, week: Week) -> bool:
        show_week(week)
        lines = {p.name: Line(p, status=self._earlier_status(week, p)) for p in self.platforms}
        try:
            to_submit = self._fill(week, lines)
            show_summary(lines)
            if to_submit and self._go_ahead(len(to_submit)):
                self._submit(week, to_submit, lines)
                show_summary(lines)
        finally:
            self._close_sessions()
        archived = self._archive_if_finished(week)
        return archived and not any(line.failed for line in lines.values())

    def pump(self) -> None:
        """Keep open browsers responsive while waiting for an answer."""
        sessions = [p.session for p in self.platforms if p.session is not None]
        if not sessions:
            time.sleep(0.2)
        for session in sessions:
            session.pump(100)

    # Steps

    def _fill(self, week: Week, lines: dict[str, Line]) -> list[Platform]:
        """Save drafts where the site has them; return what still needs a go-ahead."""
        to_submit: list[Platform] = []
        for p in self.platforms:
            if self.only is not None and p.name not in self.only:
                continue
            line = lines[p.name]
            record = self.state.record(week.key, p.name)
            status = None if self.force else self._status(week, p)
            if status == SUBMITTED:
                if record.get("hash") != week.data_hash():
                    log.warning("! %s: this week changed after it was submitted there. "
                                "Fix it there by hand, or re-run with --force.", p.title)
                continue
            if not p.has_draft:
                if self.mode == "save":
                    line.status = "skipped (this site has no drafts)"
                else:
                    line.status = "nothing written yet (no drafts here)"
                    line.pending = f"create {len(week.worked_days())} entries"
                    to_submit.append(p)
                continue
            if status != SAVED:
                if not self._step(p, line, "fill", p.fill_week, week, record["data"]):
                    continue
                self.state.mark(week.key, p.name, SAVED, week.data_hash())
            if self.mode != "save" and p.has_submit():
                line.pending = p.submit_label()
                to_submit.append(p)
        return to_submit

    def _go_ahead(self, steps: int) -> bool:
        if self.mode == "submit":
            return True
        if self.mode == "save":
            return False
        log.info("Have a look at the open Edge windows if you want to check anything.")
        answer = self.confirm(f"Go ahead with the {steps} step(s) marked '->'? [y/N] ", self.pump)
        log.debug("go-ahead answer: %s", answer)
        if not answer:
            log.info("Nothing submitted. Saved drafts stay; run again when you're ready.")
        return answer

    def _submit(self, week: Week, platforms: list[Platform], lines: dict[str, Line]) -> None:
        for p in platforms:
            data = self.state.record(week.key, p.name)["data"]
            action = p.submit if p.has_draft else p.fill_week  # without drafts, writing is submitting
            if self._step(p, lines[p.name], "submit", action, week, data):
                self.state.mark(week.key, p.name, SUBMITTED, week.data_hash())
                lines[p.name].pending = ""

    def _step(self, p: Platform, line: Line, label: str, action: Callable[..., str], *args: object) -> bool:
        try:
            self._sign_in(p)
            line.status = action(*args) or label
            return True
        except Exception as exc:  # one platform failing must not stop the others
            message = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            line.status, line.pending, line.failed = f"FAILED: {message}", "", True
            log.debug("%s %s failed\n%s", p.name, label, traceback.format_exc())
            if p.session is not None:
                for path in p.session.save_failure(self.run_dir, f"{p.name}-{label}"):
                    log.info("  saved %s", path)
            return False
        finally:
            self.state.save()  # keep whatever the adapter recorded (e.g. ids of entries it created)

    def _sign_in(self, p: Platform) -> None:
        if p.name in self._signed_in:
            return
        if p.uses_browser and p.session is None:
            if self.open_session is None:
                raise PlatformError("no browser available")
            log.info("[%s] opening Edge...", p.name)
            p.session = self.open_session(p)
        p.login()
        self._signed_in.add(p.name)

    def _close_sessions(self) -> None:
        for p in self.platforms:
            if p.session is not None:
                p.session.close()
                p.session = None
        self._signed_in.clear()

    def _archive_if_finished(self, week: Week) -> bool:
        """Copy the week to archive/ and clear it from the CSV once every platform is done with it."""
        name = self.timesheet.path.name
        waiting = [p.title for p in self.platforms if not self._finished(week, p)]
        if waiting:
            log.info("Kept this week in %s; still to finish: %s.", name, ", ".join(waiting))
            return True
        entry = self.state.week(week.key)
        if entry.get("archived_hash") != week.data_hash():
            path = self.timesheet.write_archive(week, self.settings.archive_dir)
            entry.update(archived_to=str(path), archived_hash=week.data_hash())
            self.state.save()
            log.info("Copied this week to %s", path)
        while True:
            try:
                removed = self.timesheet.remove_week(week)
                break
            except FileLockedError:
                prompt = (f"{name} is open in another program (Excel?). Close it and press Enter to retry, "
                          "or type s and Enter to skip: ")
                if not self.retry_locked(prompt):
                    log.warning("! Couldn't clear this week from %s; the next run will finish the job.", name)
                    return False
        entry["cleared"] = True
        self.state.save()
        log.info("Removed this week's %d row(s) from %s.", removed, name)
        return True

    # State

    def _status(self, week: Week, p: Platform) -> str | None:
        """What state.json says, ignoring a draft that was saved from different CSV data."""
        record = self.state.record(week.key, p.name)
        status = record.get("status")
        if status == SAVED and record.get("hash") != week.data_hash():
            return None
        return status

    def _finished(self, week: Week, p: Platform) -> bool:
        status = self._status(week, p)
        return status == SUBMITTED or (status == SAVED and (self.mode == "save" or not p.has_submit()))

    def _earlier_status(self, week: Week, p: Platform) -> str:
        status = self._status(week, p)
        return {SAVED: "draft saved earlier", SUBMITTED: "submitted earlier"}.get(status, "not done yet")


def show_week(week: Week) -> None:
    log.info("")
    log.info("Week %s to %s: %s h", week.start, week.end, week.hours_text())
    for entry in week.days:
        description = textwrap.shorten(entry.description, 70, placeholder="...") if entry.description else ""
        log.info("  %s %s %6s h  %s", WEEKDAYS[entry.day.weekday()], entry.day, entry.hours_text(), description)
    for day in week.missing_weekdays():
        log.warning("  ! %s %s has no entry", WEEKDAYS[day.weekday()], day)


def show_summary(lines: dict[str, Line]) -> None:
    log.info("")
    for line in lines.values():
        pending = f"-> {line.pending}" if line.pending else ""
        log.info("  %-16s %-44s %s", line.platform.name, line.status, pending)
    log.info("")
