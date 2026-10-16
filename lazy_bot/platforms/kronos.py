"""WWT Kronos (UKG Pro Workforce Management), My Timecard, following WWT's "My Time User Guide".

For each week: show activities; give every weekday without hours a 0 on the Home line first (the guide's rule
for holidays and sick days); add the project's PROJECT COST COLLECTION activity line if it isn't there; type
each day's hours on that line (Kronos fills the Home line itself); Save. Then attach each day's description
as an "Activity Comment" and Save again: Kronos drops comments put on hours that aren't saved yet.
Submitting means clicking Approve, after your "y".

Sign-in goes through WWT's Okta: the username and password come from .env when present, MFA is always yours.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, expect

from ..timesheet import DayEntry, Week
from .base import Platform, PlatformError, fill_if_empty

_COLUMN_FOR_HEADER = """(label) => {
  for (const cell of document.querySelectorAll('.ui-grid-header-cell')) {
    if ((cell.innerText || '').trim().startsWith(label)) {
      return [...cell.classList].find(name => name.startsWith('ui-grid-coluiGrid')) || null;
    }
  }
  return null;
}"""
_ERROR_BUTTON = re.compile(r"^Error\s+(.*?)\s*Close message$", re.S)
_COMMENT_COUNT = re.compile(r"Comments \[(\d+)\]")
NOTE_LIMIT = 250  # characters in a Kronos comment note


def _hours(text: str) -> float | None:
    text = text.strip()
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _same(current: float | None, wanted: float | None) -> bool:
    return round(current or 0, 2) == round(wanted or 0, 2) and (current is None) == (wanted is None)


class KronosPlatform(Platform):
    name = "kronos"
    title = "WWT Kronos (UKG)"
    supports_rehearsal = True

    def needs_submit(self, week: Week) -> bool:
        return self.settings.flag("APPROVE")

    def submit_label(self) -> str:
        return "Approve timecard"

    # Sign-in

    def login(self) -> None:
        self._reopened = False
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_until_signed_in(self.page.get_by_role("heading", name="My Timecard"), step=self._sign_in_step)
        self.page.get_by_role("button", name="Save").wait_for(timeout=60_000)
        show = self.page.get_by_role("button", name="Show activities")
        if show.is_visible():
            show.click()
            self.page.get_by_role("button", name="Hide activities").wait_for()
            self.page.wait_for_timeout(1500)

    def _sign_in_step(self) -> None:
        page = self.page
        if "/wfd/home" in page.url and not self._reopened:  # single sign-on lands on Home, not the timecard
            self._reopened = True
            page.wait_for_timeout(1500)
            page.goto(self.url, wait_until="domcontentloaded")
            return
        if "login.wwt.com" not in page.url:
            return
        username = page.get_by_role("textbox", name="Username")
        if username.is_visible():
            if fill_if_empty(username, self.settings.get("EMAIL")):
                keep = page.get_by_role("checkbox", name="Keep me signed in")
                if keep.is_visible() and not keep.is_checked():
                    keep.check(force=True)
                page.get_by_role("button", name="Next").click()
            return
        password = page.get_by_role("textbox", name="Password")
        if password.is_visible():
            if fill_if_empty(password, self.settings.get("PASSWORD", strip=False)):
                page.get_by_role("button", name="Verify").click()
            return
        stay = page.get_by_role("link", name="Stay signed in")
        if stay.is_visible():
            stay.click()
        # Anything else ("Verify it's you", Okta Verify) is MFA: left to the person.

    # The week

    def fill_week(self, week: Week, store: dict) -> str:
        self._open_week(week)
        self._close_messages()
        worked = {entry.day: entry for entry in week.worked_days()}
        if worked:
            self._ensure_activity_line()
        activity = self._activity_row_or_none()
        home = self._row(lambda label: label == "Home", "Home")

        # 1. Days no longer worked lose their activity hours first, so Kronos frees their Home cell.
        if activity is not None:
            for day in week.dates():
                if day not in worked and _hours(self._cell(activity, day).inner_text()) is not None:
                    self._type(self._cell(self._activity_row(), day), "", "the project line")
        # 2. The guide: a weekday you didn't work gets a 0 on the Home line before any hours are entered,
        #    and a 0 left there earlier must go once you did work that day.
        for day in week.dates()[:5]:
            shown = _hours(self._cell(home, day).inner_text())
            if day <= date.today() and day not in worked and shown != 0:
                self._type(self._cell(home, day), "0", "the Home line")
            elif day in worked and shown == 0:
                self._type(self._cell(home, day), "", "the Home line")
        # 3. Each worked day's hours on the project line.
        for day, entry in worked.items():
            cell = self._cell(self._activity_row(), day)
            if not _same(_hours(cell.inner_text()), entry.minutes / 60):
                self._type(cell, entry.hours_text(), "the project line")
        self._raise_errors()

        comment_type = self.settings.get("COMMENT", "Activity Comment")
        to_comment = [e for e in week.worked_days() if e.description] if comment_type else []
        self._kept = set()  # days whose saved comment differs from the CSV and was left as it is
        if self.rehearsal:
            typed = sum(self._comment(entry, comment_type) for entry in to_comment)
            return f"filled in, not saved ({week.hours_text()} h, {typed} comment(s) typed)"
        self._save()
        self._check_saved(week)
        # Comments only stick to saved hours, hence the second round and the second Save.
        added = sum(self._comment(entry, comment_type) for entry in to_comment)
        if added:
            self._save()
            missing = [str(entry.day) for entry in to_comment
                       if entry.day not in self._kept and not self._has_comment(entry, comment_type)]
            if missing:
                raise PlatformError(f"hours saved, but the comment didn't stick on {', '.join(missing)}")
        notes = []
        if added:
            notes.append(f"{added} comment(s) added")
        if self._kept:
            notes.append(f"{len(self._kept)} older comment(s) left as they are")
        return f"saved ({week.hours_text()} h" + (f", {'; '.join(notes)})" if notes else ")")

    def submit(self, week: Week, store: dict) -> str:
        self._open_week(week)
        page = self.page
        if page.get_by_role("button", name="Remove approval").is_enabled():
            return "already approved"
        approve = page.get_by_role("button", name="Approve", exact=True)
        if not approve.is_enabled():
            raise PlatformError("the Approve button isn't available for this week")
        self._close_messages()
        approve.click()
        confirm = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^(Yes|Approve|OK|Continue)$"))
        try:
            confirm.first.wait_for(timeout=4000)
            confirm.first.click()
        except PlaywrightError:
            pass  # no confirmation step
        expect(page.get_by_role("button", name="Remove approval")).to_be_enabled(timeout=60_000)
        self._raise_errors()
        return "approved"

    # Steps

    def _open_week(self, week: Week) -> None:
        this_monday = date.today() - timedelta(days=date.today().weekday())
        periods = {this_monday: "Current Pay Period", this_monday - timedelta(days=7): "Previous Pay Period"}
        if week.start not in periods:
            raise PlatformError(f"lazy_bot opens only this week and last week in Kronos; enter {week.label} by hand")
        page = self.page
        option = page.get_by_role("option", name=periods[week.start], exact=True)
        for _ in range(3):  # the list sometimes ignores a click while the grid is still re-rendering
            page.get_by_role("button", name=re.compile(r"^Select timeframe")).click()
            try:
                option.wait_for(state="visible", timeout=5000)
                break
            except PlaywrightError:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1500)
        option.click()
        first_day = f"{week.start:%a} {week.start:%m/%d}"
        expect(page.locator(".ui-grid-header-cell").filter(has_text=first_day).first).to_be_visible(timeout=60_000)
        page.wait_for_timeout(1500)

    def _row(self, matches, what: str) -> int:
        labels = [text.strip() for text in
                  self.page.locator(".ui-grid-render-container-left .ui-grid-row").all_inner_texts()]
        for index, label in enumerate(labels):
            if matches(label):
                return index
        raise PlatformError(f"the timecard has no {what} line (lines: {', '.join(labels)})")

    def _activity_matches(self, label: str) -> bool:
        code = self.settings.require("ACTIVITY_QUERY").rstrip("*").strip().lower()
        wanted = self.settings.get("ACTIVITY", "Project Cost Collection").lower()
        return code in label.lower() and wanted in label.lower()

    def _activity_row(self) -> int:
        return self._row(self._activity_matches, "project activity")

    def _activity_row_or_none(self) -> int | None:
        try:
            return self._activity_row()
        except PlatformError:
            return None

    def _cell(self, row: int, day: date) -> Locator:
        column = self.page.evaluate(_COLUMN_FOR_HEADER, f"{day:%a} {day:%m/%d}")
        if not column:
            raise PlatformError(f"the timecard shows no column for {day}")
        return self.page.locator(".ui-grid-render-container-body .ui-grid-row").nth(row).locator(f".ui-grid-cell.{column}")

    def _type(self, cell: Locator, text: str, where: str) -> None:
        cell.dblclick()
        editor = cell.locator("input")
        try:
            editor.wait_for(timeout=10_000)
        except PlaywrightError:
            raise PlatformError(f"Kronos doesn't let lazy_bot edit {where} there "
                                f"(the cell holds {cell.inner_text().strip() or 'nothing'})") from None
        editor.fill(text)
        editor.press("Tab")
        self.page.wait_for_timeout(600)

    def _ensure_activity_line(self) -> None:
        try:
            self._activity_row()
            return
        except PlatformError:
            pass
        page = self.page
        query = self.settings.require("ACTIVITY_QUERY")
        wanted = self.settings.get("ACTIVITY", "Project Cost Collection")
        page.get_by_role("button", name="Add activity").click()
        dialog = page.get_by_role("dialog", name="Select Activity")
        dialog.wait_for()
        dialog.get_by_role("textbox", name="Search").fill(query)
        dialog.get_by_role("button", name="Search", exact=True).click()
        items = dialog.get_by_role("treeitem")
        try:
            items.first.wait_for(timeout=30_000)
        except PlaywrightError:
            raise PlatformError(f"Kronos found no activity for KRONOS_ACTIVITY_QUERY={query}") from None
        target = items.filter(has_text=re.compile(re.escape(wanted), re.I))
        if not target.count():  # the project code is a parent: its billable child is one level down
            items.first.focus()
            page.keyboard.press("ArrowRight")
            try:
                target.first.wait_for(timeout=30_000)
            except PlaywrightError:
                found = "; ".join(t.strip().replace("\n", " ") for t in items.all_inner_texts())
                raise PlatformError(f"no activity under {query} contains {wanted!r} (found: {found})") from None
        if target.count() > 1:
            raise PlatformError(f"several activities under {query} contain {wanted!r}; make KRONOS_ACTIVITY more precise")
        target.first.focus()
        page.keyboard.press("Space")  # the radio button next to the code
        apply = dialog.get_by_role("button", name="Apply")
        expect(apply).to_be_enabled()
        apply.click()
        dialog.wait_for(state="hidden")
        page.wait_for_timeout(1500)
        self._activity_row()

    def _details(self, day: date) -> tuple[Locator, str]:
        """Right-click the day's activity cell: Kronos shows its details (status, comments, actions)."""
        self._cell(self._activity_row(), day).click(button="right")
        details = self.page.get_by_role("dialog").filter(has_text="Activity name").first
        details.wait_for(timeout=15_000)
        return details, details.inner_text()

    def _close_details(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(500)

    def _has_comment(self, entry: DayEntry, comment_type: str) -> bool:
        _, text = self._details(entry.day)
        self._close_details()
        return comment_type in text and entry.description[:NOTE_LIMIT] in text

    def _comment(self, entry: DayEntry, comment_type: str) -> bool:
        """Give the day's activity cell a `comment_type` comment holding the description. False if nothing to do."""
        page = self.page
        note_text = entry.description[:NOTE_LIMIT]
        if len(note_text) < len(entry.description):
            self.log.warning("! Kronos notes hold %d characters; the %s description is cut short there.",
                             NOTE_LIMIT, entry.day)
        details, text = self._details(entry.day)
        if comment_type in text and note_text in text:
            self._close_details()
            return False
        count = _COMMENT_COUNT.search(text)
        if count and count[1] != "0":  # Kronos shows saved notes read-only; don't pile up a second one
            self._close_details()
            self._kept.add(entry.day)
            self.log.warning("! Kronos already has a different comment on %s; left as it is (edit it there).",
                             entry.day)
            return False
        details.get_by_role("button", name="Comments and notes").filter(visible=True).last.click()
        note = page.get_by_placeholder(re.compile(r"Type a note")).first
        note.wait_for(timeout=15_000)
        editor = page.get_by_role("dialog").filter(has=page.get_by_placeholder(re.compile(r"Type a note"))).first
        chosen = editor.locator("input[role=combobox]").first
        if chosen.input_value() != comment_type:
            chosen.click()  # opens the list of comment types ("Activity Comment 1 of 38", ...)
            page.get_by_role("link", name=re.compile(rf"^{re.escape(comment_type)} \d+ of \d+$")).filter(
                visible=True).first.click(timeout=15_000)
            if chosen.input_value() != comment_type:
                raise PlatformError(f"couldn't choose the comment type {comment_type!r} (KRONOS_COMMENT)")
        note.fill(note_text)
        if self.rehearsal:
            editor.get_by_role("button", name="Cancel").click()  # rehearsing: show it, keep nothing
        else:
            editor.get_by_role("button", name="Apply").click()
        editor.wait_for(state="hidden", timeout=15_000)
        page.wait_for_timeout(800)
        self._raise_errors()
        return True

    def _save(self) -> None:
        page = self.page
        save = page.get_by_role("button", name="Save")
        if not save.is_enabled():
            return  # nothing changed
        save.click()
        expect(save).to_be_disabled(timeout=60_000)
        page.wait_for_timeout(1500)
        self._raise_errors()

    def _check_saved(self, week: Week) -> None:
        row = self._activity_row_or_none()
        if row is None:
            if week.worked_days():
                raise PlatformError("after saving, the project line is gone")
            return
        wrong = []
        for day in week.dates():
            entry = week.entry(day)
            wanted = entry.minutes / 60 if entry and entry.minutes else None
            shown = _hours(self._cell(row, day).inner_text())
            if not _same(shown, wanted):
                wrong.append(f"{day} shows {shown if shown is not None else 'nothing'}")
        if wrong:
            raise PlatformError("after saving, Kronos " + "; ".join(wrong))

    # Messages

    def _messages(self) -> list[Locator]:
        return [button for button in self.page.get_by_role("button", name=_ERROR_BUTTON).all() if button.is_visible()]

    def _close_messages(self) -> None:
        for button in self._messages():
            button.click()

    def _raise_errors(self) -> None:
        errors = []
        for button in self._messages():
            match = _ERROR_BUTTON.match(button.get_attribute("aria-label") or "")
            errors.append(match[1] if match else "an error")
        if errors:
            raise PlatformError("Kronos says: " + " | ".join(errors))
