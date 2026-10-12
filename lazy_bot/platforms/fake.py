"""A local practice timesheet (tests/fixtures/fake_timesheet.html), and the smallest complete adapter."""

from __future__ import annotations

from playwright.sync_api import expect

from ..config import PROJECT_DIR
from ..timesheet import Week
from .base import Platform, fill_if_empty

FIXTURE = PROJECT_DIR / "tests" / "fixtures" / "fake_timesheet.html"


class FakePlatform(Platform):
    name = "fake"
    title = "Fake timesheet"
    supports_rehearsal = True

    @property
    def url(self) -> str:
        return self.settings.get("URL") or FIXTURE.as_uri()

    def login(self) -> None:
        self.page.goto(self.url)
        self.wait_until_signed_in(self.page.locator("#app"), step=self._sign_in_step)

    def _sign_in_step(self) -> None:
        page = self.page
        if not page.locator("#login").is_visible():
            return
        fill_if_empty(page.locator("#email"), self.settings.get("EMAIL"))
        if fill_if_empty(page.locator("#password"), self.settings.get("PASSWORD", strip=False)):
            page.locator("#sign-in").click()

    def _open_week(self, week: Week) -> None:
        self.page.locator("#week").fill(week.start.isoformat())

    def fill_week(self, week: Week, store: dict) -> str:
        page = self.page
        self._open_week(week)
        for day in week.dates():
            entry = week.entry(day)
            page.get_by_label(f"Hours {day}", exact=True).fill(entry.hours_text() if entry else "")
            page.get_by_label(f"Note {day}", exact=True).fill(entry.description if entry else "")
        if self.rehearsal:
            return "filled in, not saved"
        page.locator("#save").click()
        expect(page.locator("#status")).to_have_text("Draft saved")
        return f"draft saved ({week.hours_text()} h)"

    def submit(self, week: Week, store: dict) -> str:
        page = self.page
        self._open_week(week)
        page.locator("#submit").click()
        expect(page.locator("#status")).to_have_text("Submitted")
        return "submitted"
