"""Drives the real Edge (headless) through a whole run against the local fake timesheet."""

import pytest

from lazy_bot.browser import BrowserPool, find_edge
from lazy_bot.platforms import make_platform
from lazy_bot.platforms.fake import FIXTURE
from lazy_bot.runner import Runner
from lazy_bot.snapshots import Snapshotter
from lazy_bot.state import State
from lazy_bot.timesheet import read_timesheet

pytestmark = pytest.mark.edge


@pytest.fixture(autouse=True)
def needs_edge():
    try:
        find_edge()
    except FileNotFoundError:
        pytest.skip("Microsoft Edge is not installed")


def run_once(settings, rehearse=False):
    state = State.load(settings.state_path)
    timesheet = read_timesheet(settings.csv_path, settings.date_formats)
    platform = make_platform(settings, "fake", state.cache("fake"))
    with BrowserPool(settings) as pool:
        runner = Runner(settings, state, timesheet, [platform], mode=settings.submit_mode,
                        run_dir=settings.logs_dir / "run", open_session=pool.open, rehearse=rehearse)
        return runner.run(timesheet.weeks()[:1])


def site_data(settings):
    with BrowserPool(settings) as pool:
        platform = make_platform(settings, "fake", {})
        session = pool.open(platform)
        session.page.goto(FIXTURE.as_uri())
        data = session.page.evaluate("JSON.parse(localStorage.getItem('fake-timesheet'))")
        session.close()
    return data


def test_saved_pages_never_contain_passwords(make_settings, tmp_path):
    settings = make_settings(HEADLESS="true", FAKE_PASSWORD="known-from-env-file")
    typed = "typed-into-the-page"
    with BrowserPool(settings) as pool:
        session = pool.open(make_platform(settings, "fake", {}))
        session.page.goto(FIXTURE.as_uri())
        session.page.locator("#password").fill(typed)
        session.page.evaluate("document.title = 'known-from-env-file'")  # as if a page echoed it back
        Snapshotter(session, tmp_path / "snaps").capture()
        session.save_page(tmp_path / "failure", "fake-fill")
        session.close()
    saved = [p for folder in ("snaps", "failure") for p in (tmp_path / folder).iterdir() if p.suffix != ".png"]
    assert len(saved) >= 4
    for path in saved:
        text = path.read_text(encoding="utf-8")
        assert typed not in text and "known-from-env-file" not in text, path.name


def test_whole_run_in_edge(make_settings, write_csv):
    write_csv()
    settings = make_settings(PLATFORMS="fake", SUBMIT_MODE="submit", HEADLESS="true", LOGIN_TIMEOUT_SECONDS="30",
                             FAKE_EMAIL="me@example.com", FAKE_PASSWORD="not-a-real-password")
    assert run_once(settings, rehearse=True)
    assert site_data(settings)["sheets"] == {}  # rehearsal: typed in, never saved
    assert (settings.logs_dir / "run" / "fake-rehearsal.png").exists()
    assert not settings.archive_dir.exists()

    assert run_once(settings)

    sheet = site_data(settings)["sheets"]["2026-10-05"]
    assert sheet["status"] == "Submitted"
    assert sheet["days"]["2026-10-06"] == {"hours": "7.5", "note": "Code review"}
    assert sorted(sheet["days"]) == [f"2026-10-0{d}" for d in range(5, 10)]
    assert (settings.archive_dir / "2026-10-05_to_2026-10-11.csv").exists()
    assert settings.csv_path.read_text(encoding="utf-8").splitlines() == ["date,hours,description",
                                                                          "2026-10-12,4,Next week already"]
