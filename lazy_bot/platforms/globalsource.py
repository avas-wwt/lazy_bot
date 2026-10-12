"""GlobalSource: Deel under Cloud Decisions branding. Deel has no drafts, so entries are written only after "y"."""

from __future__ import annotations

from ..timesheet import Week
from .base import Platform, not_written


class GlobalSourcePlatform(Platform):
    name = "globalsource"
    title = "GlobalSource (Deel)"
    has_draft = False

    def login(self) -> None:
        raise not_written(self)

    def fill_week(self, week: Week, store: dict) -> str:
        raise not_written(self)
