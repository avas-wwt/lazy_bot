"""GlobalSource: Deel under Cloud Decisions branding, used through Deel's own address (app.deel.com).

Deel has no drafts: a submitted day goes straight to approval, so lazy_bot writes here only after your "y".
Each worked day is its own submission ("Submit hours on multiple days" with that one day selected): start at
GLOBALSOURCE_DAY_START, end after the day's hours, with the day's description. Days that already have hours in
Deel are left alone; a different amount there only produces a warning.

Safeguards agreed for Deel: lazy_bot never types your Deel password (you sign in in the window), does nothing
to look less automated, and if Deel shows any verification step or warning it stops and leaves the window to
you. Keep GlobalSource manual from then on.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, expect

from ..config import ConfigError
from ..timesheet import DayEntry, Week
from .base import Platform, PlatformError

_CHALLENGE = re.compile(r"verify (that )?you('| a)re (a )?human|unusual activity|suspicious|captcha|security check|"
                        r"automated (access|activity|traffic)", re.I)
_CAPTCHA_FRAMES = ("hcaptcha", "recaptcha", "challenges.cloudflare", "turnstile", "arkoselabs", "funcaptcha")
_ROW_HOURS = re.compile(r"(\d+(?:\.\d+)?) hrs")


def ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def row_label(day: date) -> str:
    """How Deel's table names a day: 'Mon Oct 12th'."""
    return f"{day:%a} {day:%b} {ordinal(day.day)}"


class GlobalSourcePlatform(Platform):
    name = "globalsource"
    title = "GlobalSource (Deel)"
    has_draft = False
    supports_rehearsal = True  # fills each day's form up to the review step, then cancels

    # Sign-in (by the person, always)

    def login(self) -> None:
        page = self.page
        page.goto(self.url, wait_until="domcontentloaded")
        told = False
        deadline = datetime.now() + timedelta(seconds=self.app.login_timeout)
        while not page.get_by_role("heading", name="Time tracking").is_visible():
            self._stop_on_challenge()
            if "/login" in page.url and not told:
                self.log.info("[%s] Sign in to Deel in the Edge window (lazy_bot doesn't type Deel passwords); "
                              "waiting up to %d min...", self.name, round(self.app.login_timeout / 60))
                told = True
            if datetime.now() > deadline:
                raise PlatformError("not signed in to Deel in time (sign in in the Edge window, "
                                    "or raise LOGIN_TIMEOUT_SECONDS)")
            page.wait_for_timeout(1000)
        page.get_by_role("button", name="Submit hours").wait_for(timeout=60_000)
        self._stop_on_challenge()

    # The week (called only after the go-ahead, or while rehearsing)

    def fill_week(self, week: Week, store: dict) -> str:
        submitted, skipped = [], []
        for entry in week.worked_days():
            self._open_pay_cycle(entry.day)
            shown = self._shown_hours(entry.day)
            if shown is not None:
                if round(shown, 2) != round(entry.hours, 2):
                    self.log.warning("! Deel already has %s h on %s (CSV says %s h); left as it is.",
                                     _hours_text(shown), entry.day, entry.hours_text())
                skipped.append(entry.day)
                continue
            self._submit_day(entry)
            submitted.append(entry.day)
        parts = [f"{len(submitted)} {'rehearsed' if self.rehearsal else 'submitted'}"]
        if skipped:
            parts.append(f"{len(skipped)} already there")
        return ", ".join(parts)

    # Steps

    def _open_pay_cycle(self, day: date) -> None:
        """Show the pay cycle (month) that contains `day`."""
        page = self.page
        month = f"{day:%b} 1st"  # e.g. "Oct 1st - Oct 31st 2026"
        cycle = page.get_by_role("button", name=re.compile(rf"^{month} - .* {day.year}$"))
        if cycle.count():
            return
        page.get_by_role("button", name=re.compile(r"^\w{3} \d+\w{2} - \w{3} \d+\w{2} \d{4}$")).first.click()
        option = page.get_by_role("option", name=re.compile(rf"^{month} - .* {day.year}$")).or_(
            page.get_by_role("menuitem", name=re.compile(rf"^{month} - .* {day.year}$")))
        try:
            option.first.click(timeout=10_000)
        except PlaywrightError:
            raise PlatformError(f"Deel doesn't offer the pay cycle for {calendar.month_name[day.month]} {day.year} yet")
        expect(cycle).to_be_visible(timeout=30_000)
        page.wait_for_timeout(1500)

    def _row(self, day: date) -> Locator:
        return self.page.get_by_role("row").filter(
            has=self.page.get_by_role("cell", name=row_label(day), exact=True)).first

    def _shown_hours(self, day: date) -> float | None:
        row = self._row(day)
        row.wait_for(timeout=30_000)
        text = row.inner_text()
        if "No hours worked" in text:
            return None
        match = _ROW_HOURS.search(text)
        return float(match[1]) if match else 0.0

    def _submit_day(self, entry: DayEntry) -> None:
        page = self.page
        start = datetime.combine(entry.day, self._day_start())
        end = start + timedelta(minutes=entry.minutes)
        if end.date() != entry.day:
            raise PlatformError(f"{entry.day}: {entry.hours_text()} h from {start:%H:%M} would pass midnight")

        page.get_by_role("button", name="Submit hours").click()
        page.get_by_role("menuitem", name="Submit hours on multiple days").click()
        dialog = page.get_by_role("dialog").filter(has_text="Submit hours on multiple days")
        dialog.wait_for(timeout=30_000)
        self._stop_on_challenge()

        self._calendar_month(dialog, entry.day)
        day_cell = dialog.get_by_role("gridcell", name=str(entry.day.day), exact=True)
        if day_cell.get_attribute("aria-disabled") == "true" or day_cell.is_disabled():
            raise PlatformError(f"Deel won't take hours for {entry.day} (it greys that day out)")
        day_cell.click()
        expect(dialog.get_by_role("gridcell", name=str(entry.day.day), exact=True)).to_have_attribute(
            "aria-selected", "true")

        times = dialog.get_by_role("checkbox", name=re.compile(r"^Submit start and end times"))
        if not times.is_checked():
            times.check()
        self._type_time(dialog.get_by_role("textbox", name="Start time"), f"{start:%H:%M}")
        self._type_time(dialog.get_by_role("textbox", name="End time"), f"{end:%H:%M}")
        dialog.get_by_role("textbox", name="Work description (optional)").fill(entry.description)

        dialog.get_by_role("button", name="Continue").click()
        review = page.get_by_role("dialog").filter(has_text="Review the work time details")
        review.wait_for(timeout=30_000)
        self._stop_on_challenge()
        text = " ".join(review.inner_text().split())
        expected = [f"{entry.day:%b} {ordinal(entry.day.day)}", f"{start:%H:%M} - {end:%H:%M}", entry.description]
        missing = [part for part in expected if part not in text]
        if missing:
            review.get_by_role("button", name="Cancel").click()
            raise PlatformError(f"Deel's review for {entry.day} doesn't show {missing}; nothing submitted")

        if self.rehearsal:
            review.get_by_role("button", name="Cancel").click()
            review.wait_for(state="hidden", timeout=30_000)
            return
        review.get_by_role("button", name="Submit", exact=True).click()
        review.wait_for(state="hidden", timeout=60_000)
        self._stop_on_challenge()
        expect(self._row(entry.day)).not_to_contain_text("No hours worked", timeout=60_000)

    def _calendar_month(self, dialog: Locator, day: date) -> None:
        label = f"{calendar.month_name[day.month]} {day.year}"
        for _ in range(12):
            if dialog.get_by_role("grid", name=label).count():
                return
            shown = dialog.get_by_role("grid").first.get_attribute("aria-label") or ""
            try:
                shown_month = datetime.strptime(shown, "%B %Y").date()
            except ValueError:
                raise PlatformError(f"Deel's calendar shows {shown!r}, expected {label}") from None
            dialog.get_by_role("button", name="Next month" if shown_month < day.replace(day=1) else "Previous month").click()
            self.page.wait_for_timeout(500)
        raise PlatformError(f"Deel's calendar never showed {label}")

    def _type_time(self, box: Locator, value: str) -> None:
        box.click()
        box.press("Control+A")
        box.press_sequentially(value)  # masked hh:mm field: it needs real key presses
        expect(box).to_have_value(value)

    def _day_start(self):
        try:
            return datetime.strptime(self.settings.get("DAY_START", "09:00"), "%H:%M").time()
        except ValueError:
            raise ConfigError(f"GLOBALSOURCE_DAY_START={self.settings.get('DAY_START')!r} should look like 09:00") from None

    def _stop_on_challenge(self) -> None:
        """Agreed safeguard: any verification step or warning means stop, and the person takes over."""
        page = self.page
        challenge = next((f.url for f in page.frames if any(k in f.url for k in _CAPTCHA_FRAMES)), None)
        if challenge is None:
            for dialog in page.get_by_role("dialog").all() + page.get_by_role("alert").all():
                try:
                    text = dialog.inner_text()
                except PlaywrightError:
                    continue
                if _CHALLENGE.search(text):
                    challenge = " ".join(text.split())[:160]
                    break
        if challenge:
            self.log.warning("! Deel is asking for extra verification (%s). lazy_bot stops here: finish in the "
                             "Edge window, then close it, and keep GlobalSource manual from now on.", challenge)
            if self.session is not None:
                self.session.wait_until_closed()
            raise PlatformError("Deel asked for extra verification; stopped (enter Deel hours by hand)")


def _hours_text(hours: float) -> str:
    return f"{hours:.2f}".rstrip("0").rstrip(".")
