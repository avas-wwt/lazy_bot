"""WWT Kronos (UKG Pro Workforce Management), My Timecard. Signs in through UKG, then WWT's Okta."""

from __future__ import annotations

from ..timesheet import Week
from .base import Platform, not_written


class KronosPlatform(Platform):
    name = "kronos"
    title = "WWT Kronos (UKG)"

    def has_submit(self) -> bool:
        return self.settings.flag("APPROVE")

    def submit_label(self) -> str:
        return "Approve timecard"

    def login(self) -> None:
        raise not_written(self)

    def fill_week(self, week: Week, store: dict) -> str:
        raise not_written(self)

    def submit(self, week: Week, store: dict) -> str:
        raise not_written(self)
