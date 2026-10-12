"""Clockify, through its REST API (no browser)."""

from __future__ import annotations

from ..timesheet import Week
from .base import Platform, not_written


class ClockifyPlatform(Platform):
    name = "clockify"
    title = "Clockify"
    uses_browser = False

    def login(self) -> None:
        raise not_written(self)

    def fill_week(self, week: Week, store: dict) -> str:
        raise not_written(self)

    def submit(self, week: Week, store: dict) -> str:
        raise not_written(self)
