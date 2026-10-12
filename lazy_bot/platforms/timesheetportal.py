"""Timesheet Portal (Anfold Software), as Cloud Decisions UK uses it: one timesheet per month.

Each run writes the week's days into the month's draft (a week across a month end touches two timesheets):
the hours as HH:MM and the day's description as that day's note, then "Save draft". The month itself is
submitted, after your "y", in the week that contains its last day.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect

from ..timesheet import DayEntry, Week
from .base import Platform, PlatformError, fill_if_empty

NOTE_PLACEHOLDER = "[Enter Timesheet Note]"
NOTE_LIMIT = 500  # characters per day note
STATUSES = ("Draft", "Submitted", "Partially Approved", "Approved", "Rejected", "Exported", "Cancelled")
_BLOCKED = re.compile(r"dialogMessage\('([^']*)'")


def month_ends(week: Week) -> list[date]:
    """The last days of months that fall inside the week."""
    return [day for day in week.dates() if (day + timedelta(days=1)).month != day.month]


def clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class TimesheetPortalPlatform(Platform):
    name = "timesheetportal"
    title = "Timesheet Portal (Cloud Decisions)"
    supports_rehearsal = True

    def needs_submit(self, week: Week) -> bool:
        return bool(month_ends(week))

    def submit_label(self) -> str:
        return "Submit the month's timesheet"

    # Sign-in

    def login(self) -> None:
        self._password_typed = False
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_until_signed_in(self.page.get_by_role("link", name="My timesheets"), step=self._sign_in_step)

    def _sign_in_step(self) -> None:
        page = self.page
        username = page.locator("#txtUsername")
        if not username.is_visible():
            return
        fill_if_empty(username, self.settings.get("EMAIL"))
        secret = self.settings.get("PASSWORD", strip=False)
        if secret and username.input_value() and not self._password_typed:
            self._password_typed = True  # once only: if it's refused, you take over
            password = page.locator("#txtAuthToken")
            password.click()  # the box stays read-only until it has focus
            password.fill(secret)
            keep = page.get_by_role("checkbox", name="Keep me logged in")
            if keep.is_visible() and not keep.is_checked():
                keep.check(force=True)
            page.locator("#btnLogin").click()

    # The week

    def fill_week(self, week: Week, store: dict) -> str:
        parts = []
        for (year, month), days in self._by_month(week).items():
            self._open_month(year, month)
            changed = [day for day in days if self._set_day(day, week.entry(day))]
            self._raise_dialogs()
            if changed and not self.rehearsal:
                self._save_draft()
                self._check(year, month, days, week)
            parts.append(f"{calendar.month_abbr[month]}: {len(changed)} day(s) changed")
        verb = "filled in, not saved" if self.rehearsal else "draft saved"
        return f"{verb} ({'; '.join(parts)})"

    def submit(self, week: Week, store: dict) -> str:
        page = self.page
        done = []
        for last_day in month_ends(week):
            name = calendar.month_name[last_day.month]
            self._open_month(last_day.year, last_day.month)
            status = self._status()
            if status != "Draft":
                done.append(f"{name} already {status.lower()}")
                continue
            if approver := self.settings.get("APPROVER"):
                self._choose_approver(approver)
            button = page.locator("[name$=btnSubmit]").first
            blocked = _BLOCKED.search(button.get_attribute("onclick") or "")
            if blocked and "cannot submit" in blocked[1].lower():
                raise PlatformError(f"Timesheet Portal says: {blocked[1]}")
            button.click()
            confirm = page.locator(".ui-dialog:visible").get_by_role("button", name=re.compile(r"^(Yes|OK|Submit|Confirm)$"))
            try:
                confirm.first.wait_for(timeout=4000)
                confirm.first.click()
            except PlaywrightError:
                pass  # no confirmation step
            page.wait_for_load_state("load")
            self._raise_dialogs()
            expect(page.get_by_text(re.compile(r"^(Submitted|Partially Approved|Approved)$")).first).to_be_visible(
                timeout=60_000)
            done.append(f"{name} submitted")
        return "; ".join(done)

    # Steps

    @staticmethod
    def _by_month(week: Week) -> dict[tuple[int, int], list[date]]:
        months: dict[tuple[int, int], list[date]] = {}
        for day in week.dates():
            months.setdefault((day.year, day.month), []).append(day)
        return months

    def _open_month(self, year: int, month: int) -> None:
        """'My timesheets' opens this month's timesheet; Previous/Next walk to the one wanted."""
        page = self.page
        page.get_by_role("link", name="My timesheets").click()
        page.wait_for_load_state("domcontentloaded")
        caption = page.locator("#MainContent_lblDateCaption")
        caption.wait_for(timeout=60_000)
        today = date.today()
        steps = (year * 12 + month) - (today.year * 12 + today.month)
        for _ in range(abs(steps)):
            shown = caption.inner_text().strip()
            page.get_by_role("button", name="Next" if steps > 0 else "Previous").click()
            expect(caption).not_to_have_text(shown, timeout=60_000)
        shown = caption.inner_text().strip()
        if shown != calendar.month_name[month]:
            raise PlatformError(f"Timesheet Portal shows {shown}, not {calendar.month_name[month]} {year}")
        page.locator("input[id^=ctlEnd_]").first.wait_for(timeout=60_000)

    def _box(self, day: date):
        return self.page.locator(f"#ctlEnd_1_{day.day - 1}_0")

    def _set_day(self, day: date, entry: DayEntry | None) -> bool:
        """Set the day's hours and note. True when something changed."""
        page = self.page
        minutes = entry.minutes if entry else 0
        wanted_note = entry.description[:NOTE_LIMIT] if entry and minutes else ""
        if entry and minutes and len(entry.description) > NOTE_LIMIT:
            self.log.warning("! Timesheet Portal notes hold %d characters; the %s note is cut short.", NOTE_LIMIT, day)
        box = self._box(day)
        box.click()  # also opens the day's note panel
        page.wait_for_timeout(500)
        changed = False
        if box.input_value() != clock(minutes):
            box.fill(clock(minutes))
            changed = True
        note = page.locator("#txtEditNoteMessage")
        if not note.is_visible():
            if wanted_note:
                raise PlatformError(f"the note box for {day} didn't open")
            return changed
        current_note = note.input_value()
        if current_note == NOTE_PLACEHOLDER:
            current_note = ""
        if current_note != wanted_note:
            note.click()
            note.fill(wanted_note)
            note.press("End")  # the page records a note on key-up, not on input
            changed = True
        return changed

    def _save_draft(self) -> None:
        page = self.page
        page.locator("[id$=btnSaveTimesheet]").click()
        page.wait_for_load_state("load")
        saving = page.locator("[id$=pnlHiddenSaving]")
        try:
            expect(saving).to_be_hidden(timeout=60_000)
        except AssertionError:
            raise PlatformError("Timesheet Portal kept saying 'Saving...' for a minute") from None
        page.wait_for_timeout(1500)
        self._raise_dialogs()

    def _check(self, year: int, month: int, days: list[date], week: Week) -> None:
        self._open_month(year, month)  # read back what the portal really kept
        wrong = []
        for day in days:
            entry = week.entry(day)
            minutes = entry.minutes if entry else 0
            box = self._box(day)
            shown = box.input_value()
            if shown != clock(minutes):
                wrong.append(f"{day} shows {shown}")
            elif entry and minutes and entry.description:
                classes = box.evaluate("e => e.parentElement.className") or ""
                if "noteAvailable" not in classes:
                    wrong.append(f"{day} has no note")
        if wrong:
            raise PlatformError("after saving, Timesheet Portal " + "; ".join(wrong))

    def _status(self) -> str:
        found = self.page.get_by_text(re.compile(rf"^({'|'.join(STATUSES)})$")).first
        return found.inner_text().strip() if found.count() else "unknown"

    def _choose_approver(self, approver: str) -> None:
        page = self.page
        search = page.locator("[id$=cmbApprovers_CC_1_txtSearch]")
        if search.input_value().strip().lower() == approver.lower():
            return
        search.click()
        search.fill(approver)
        item = page.locator(".ui-menu-item-wrapper:visible").filter(has_text=re.compile(rf"^\s*{re.escape(approver)}\s*$"))
        try:
            item.first.click(timeout=10_000)
        except PlaywrightError:
            raise PlatformError(f"Timesheet Portal has no approver called {approver!r} (TIMESHEETPORTAL_APPROVER)") from None

    def _raise_dialogs(self) -> None:
        dialogs = [d.inner_text().strip() for d in self.page.locator(".ui-dialog:visible").all()]
        if dialogs:
            raise PlatformError("Timesheet Portal says: " + " | ".join(" ".join(d.split()) for d in dialogs))
