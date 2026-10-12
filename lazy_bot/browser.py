"""Opens a platform's own Edge profile: launched by Playwright, or started normally and attached over CDP."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .config import Settings

if TYPE_CHECKING:
    from .platforms.base import Platform

log = logging.getLogger("lazy_bot.browser")
VIEWPORT = {"width": 1366, "height": 900}  # UKG switches to a list view below 768 px wide
BROWSER_MODES = ("launch", "attach")
SECRET_WORDS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY")
_TYPED_PASSWORDS = "() => Array.from(document.querySelectorAll('input[type=password]'), i => i.value).filter(Boolean)"


def page_secrets(page: Page, known: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Passwords typed on the page (in any frame) plus `known` ones, longest first, for blanking out of dumps."""
    found = [secret for secret in known if secret]
    for frame in page.frames:
        try:
            found += frame.evaluate(_TYPED_PASSWORDS)
        except PlaywrightError:
            pass
    return sorted(set(found), key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if len(secret) >= 4:  # shorter values would blank out ordinary text
            text = text.replace(secret, "[redacted]")
    return text


def find_edge() -> Path:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            exe = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if exe.exists():
                return exe
    raise FileNotFoundError("Microsoft Edge (msedge.exe) was not found")


class BrowserSession:
    """One Edge window on one platform's persistent profile, so its sign-in survives between runs."""

    def __init__(self, playwright: Playwright, profile_dir: Path, *, headless: bool = False,
                 mode: str = "launch", args: tuple[str, ...] = ()):
        if mode not in BROWSER_MODES:
            raise ValueError(f"browser mode {mode!r} should be one of: {', '.join(BROWSER_MODES)}")
        self.playwright = playwright
        self.profile_dir = profile_dir
        self.headless = headless
        self.mode = mode
        self.args = list(args)
        self.secrets: list[str] = []  # the platform's passwords/keys, never written into saved pages
        self.context: BrowserContext | None = None
        self._page: Page | None = None
        self._browser = None
        self._process: subprocess.Popen | None = None

    def open(self) -> Page:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if self.mode == "attach":
            self.context = self._attach()
        else:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile_dir), channel="msedge", headless=self.headless, viewport=VIEWPORT, args=self.args
            )
        self.context.set_default_timeout(30_000)
        return self.page

    @property
    def page(self) -> Page:
        if self.context is None:
            raise RuntimeError("the browser session is not open")
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            self._page = open_pages[0] if open_pages else self.context.new_page()
        return self._page

    def is_open(self) -> bool:
        if self.context is None:
            return False
        try:
            return any(not p.is_closed() for p in self.context.pages)
        except PlaywrightError:
            return False

    def pump(self, ms: int = 200) -> None:
        """Wait while letting Playwright handle browser events (plain sleeps leave them queued)."""
        try:
            self.page.wait_for_timeout(ms)
        except (PlaywrightError, RuntimeError):
            time.sleep(ms / 1000)

    def wait_until_closed(self) -> None:
        """Block until the person closes every window of this profile."""
        while self.is_open():
            try:
                next(p for p in self.context.pages if not p.is_closed()).wait_for_timeout(500)
            except (PlaywrightError, StopIteration):
                pass

    def save_failure(self, folder: Path, label: str) -> list[Path]:
        """Screenshot and HTML (passwords blanked) of the current page, for working out what went wrong."""
        saved: list[Path] = []
        if self.context is None:
            return saved
        try:
            folder.mkdir(parents=True, exist_ok=True)
            shot = folder / f"{label}.png"
            self.page.screenshot(path=str(shot), full_page=True)
            saved.append(shot)
            html = folder / f"{label}.html"
            html.write_text(redact(self.page.content(), page_secrets(self.page, self.secrets)), encoding="utf-8")
            saved.append(html)
        except Exception as exc:  # the window may already be gone; the log still has the error
            log.debug("could not save failure files: %s", exc)
        return saved

    def close(self) -> None:
        try:
            if self._browser is not None:
                # Ask Edge to quit cleanly so it writes the sign-in cookies to the profile.
                self._browser.new_browser_cdp_session().send("Browser.close")
            elif self.context is not None:
                self.context.close()
        except PlaywrightError as exc:
            log.debug("closing the browser: %s", exc)
        finally:
            if self._process is not None:
                try:
                    self._process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
            self.context = self._page = self._browser = self._process = None

    def _attach(self) -> BrowserContext:
        """Start Edge ourselves (no automation switches) and connect to it on a local debugging port."""
        port = _free_port()
        window = f"--window-size={VIEWPORT['width'] + 20},{VIEWPORT['height'] + 140}"
        self._process = subprocess.Popen([
            str(find_edge()), f"--remote-debugging-port={port}", f"--user-data-dir={self.profile_dir}",
            "--no-first-run", "--no-default-browser-check", window, *self.args, "about:blank",
        ])
        endpoint = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never send this to a proxy
        deadline = time.monotonic() + 30
        while True:
            try:
                opener.open(f"{endpoint}/json/version", timeout=2).close()
                break
            except OSError:
                if self._process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Edge didn't open its debugging port. Close any other Edge window that uses "
                                       f"this profile ({self.profile_dir}) and try again.") from None
                time.sleep(0.5)
        self._browser = self.playwright.chromium.connect_over_cdp(endpoint)
        return self._browser.contexts[0]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BrowserPool:
    """Starts Playwright the first time a browser is needed and opens one profile per platform."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright: Playwright | None = None

    def open(self, platform: Platform) -> BrowserSession:
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        options = platform.settings
        session = BrowserSession(
            self._playwright,
            self.settings.profile_dir(platform.name),
            headless=self.settings.headless,
            mode=options.get("BROWSER_MODE", "launch").lower(),
            args=tuple(options.get("BROWSER_ARGS").split()),
        )
        session.secrets = [value for key, value in options.values.items()
                           if value and any(word in key for word in SECRET_WORDS)]
        session.open()
        return session

    def __enter__(self) -> BrowserPool:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
