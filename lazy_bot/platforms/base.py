"""The interface every platform adapter implements. fake.py is the smallest complete example."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from ..config import PlatformSettings, Settings
from ..timesheet import Week

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

    from ..browser import BrowserSession


class PlatformError(Exception):
    """A step failed in a way the person can fix; the message is shown as-is in the summary."""


class Platform:
    name: ClassVar[str]  # the PLATFORMS entry, the .env prefix (KRONOS_...) and the state.json key
    title: ClassVar[str]  # shown to the person
    uses_browser: ClassVar[bool] = True
    has_draft: ClassVar[bool] = True  # False when writing an entry already submits it (Deel)
    supports_rehearsal: ClassVar[bool] = False  # can fill_week() run with self.rehearsal set?

    def __init__(self, settings: PlatformSettings, app: Settings, cache: dict):
        self.settings = settings
        self.app = app
        self.cache = cache  # kept in state.json across weeks
        self.session: BrowserSession | None = None
        self.rehearsal = False  # set by `run --rehearse`: fill everything in, but never save or submit
        self.log = logging.getLogger(f"lazy_bot.{self.name}")

    @property
    def url(self) -> str:
        return self.settings.require("URL")

    def needs_submit(self, week: Week) -> bool:
        """Whether this week has a step after saving (Submit, Approve...). If not, a saved week counts as done."""
        return True

    def submit_label(self) -> str:
        return "Submit"

    def describe(self) -> str:
        """What login() found (account, workspace...), shown by `lazy_bot login`. Empty if nothing to say."""
        return ""

    def login(self) -> None:
        """Open the site and make sure we're signed in. MFA is always left to the person."""
        raise NotImplementedError

    def fill_week(self, week: Week, store: dict) -> str:
        """Set (never add to) each day's hours and description, then save. Returns a short summary.

        `store` is this platform's slot for the week in state.json, e.g. for ids of created entries.
        """
        raise NotImplementedError

    def submit(self, week: Week, store: dict) -> str:
        """Submit a week that fill_week() saved. Re-open the week first: the session may have expired."""
        raise NotImplementedError

    @property
    def page(self) -> Page:
        if self.session is None:
            raise PlatformError(f"{self.title} needs a browser")
        return self.session.page

    def wait_until_signed_in(self, ready: Locator, step: Callable[[], None] | None = None) -> None:
        """Wait until `ready` is visible. `step` runs on every check to fill whatever sign-in screen shows."""
        from playwright.sync_api import Error as PlaywrightError

        started = time.monotonic()
        told = False
        while True:
            try:
                if ready.first.is_visible():
                    return
                if step is not None:
                    step()
            except PlaywrightError as exc:  # the page navigated in the middle of a check
                self.log.debug("sign-in check: %s", exc)
            waited = time.monotonic() - started
            if not told and waited > 5:
                self.log.info("[%s] Finish signing in in the Edge window (waiting up to %d min)...",
                              self.name, round(self.app.login_timeout / 60))
                told = True
            if waited > self.app.login_timeout:
                raise PlatformError(f"not signed in after {round(self.app.login_timeout)} s "
                                    "(finish MFA in the Edge window, or raise LOGIN_TIMEOUT_SECONDS)")
            self.page.wait_for_timeout(500)


def fill_if_empty(locator: Locator, value: str) -> bool:
    """Type `value` into a visible, empty field. Returns True when it typed something."""
    if value and locator.is_visible() and not locator.input_value():
        locator.fill(value)
        return True
    return False


def not_written(platform: Platform) -> PlatformError:
    return PlatformError(f"the {platform.title} adapter isn't written yet")
