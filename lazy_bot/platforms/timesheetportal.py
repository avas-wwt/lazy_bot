"""Timesheet Portal (Anfold Software), used by Cloud Decisions UK."""

from __future__ import annotations

from ..timesheet import Week
from .base import Platform, not_written


class TimesheetPortalPlatform(Platform):
    name = "timesheetportal"
    title = "Timesheet Portal (Cloud Decisions)"

    def login(self) -> None:
        raise not_written(self)

    def fill_week(self, week: Week, store: dict) -> str:
        raise not_written(self)

    def submit(self, week: Week, store: dict) -> str:
        raise not_written(self)
