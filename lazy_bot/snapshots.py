"""Saves what a page looks like (HTML, accessibility tree, screenshot) so adapters can be written and fixed."""

from __future__ import annotations

import hashlib
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .browser import BrowserSession


class Snapshotter:
    def __init__(self, session: BrowserSession, folder: Path):
        self.session = session
        self.folder = folder
        self.count = 0
        self._last: dict[int, str] = {}

    def capture(self, only_changes: bool = False) -> int:
        """Save every open tab (or only the tabs that changed). Returns how many were saved."""
        saved = 0
        pages = [p for p in self.session.context.pages if not p.is_closed()] if self.session.context else []
        for tab, page in enumerate(pages):
            try:
                aria = page.locator("body").aria_snapshot()
            except PlaywrightError:
                aria = ""
            fingerprint = hashlib.sha256(f"{page.url}\n{aria}".encode()).hexdigest()
            if only_changes and self._last.get(tab) == fingerprint:
                continue
            self._last[tab] = fingerprint
            self.count += 1
            saved += 1
            self._save(page, f"{self.count:03d}-tab{tab}", aria)
        return saved

    def _save(self, page, stem: str, aria: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            frames = "\n".join(f"  {i}: {frame.url}" for i, frame in enumerate(page.frames))
            (self.folder / f"{stem}.txt").write_text(f"url: {page.url}\ntitle: {page.title()}\nframes:\n{frames}\n",
                                                    encoding="utf-8")
            (self.folder / f"{stem}.aria.yaml").write_text(aria, encoding="utf-8")
            (self.folder / f"{stem}.html").write_text(page.content(), encoding="utf-8")
            for i, frame in enumerate(page.frames[1:], start=1):
                (self.folder / f"{stem}-frame{i}.html").write_text(frame.content(), encoding="utf-8")
            page.screenshot(path=str(self.folder / f"{stem}.png"), full_page=True)
        except PlaywrightError as exc:  # the page changed while saving; keep what we have
            (self.folder / f"{stem}.error.txt").write_text(str(exc), encoding="utf-8")
