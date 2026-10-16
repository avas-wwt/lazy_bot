"""Drives the real Edge (headless) through a whole run against the local fake timesheet."""

from urllib.parse import parse_qs, urlsplit

import pytest

from lazy_bot.browser import BrowserPool, find_edge
from lazy_bot.platforms import PlatformError, make_platform
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


DEEL_LOGIN = """<!doctype html><title>Deel</title><p>Log in</p>
<label>Email<input aria-label="Email" id="email" value="{prefill}"></label>
<label>Password<input aria-label="Password" type="password" id="pw"></label>
<button id="go" onclick="location.href = '{target}?submit=1&email=' + encodeURIComponent(email.value)
  + '&pw=' + encodeURIComponent(pw.value)">Log in</button>"""
DEEL_TRACKING = "<!doctype html><title>Deel</title><h1>Time tracking</h1><button>Submit hours</button>"


def fake_deel(session, *, accepts: bool, prefill: str = ""):
    """Serve a stand-in for Deel's login and Time tracking pages; returns the log of login submissions."""
    submissions: list[dict] = []

    def page(body: str):
        return {"status": 200, "content_type": "text/html", "body": body}

    def handle(route):
        url = urlsplit(route.request.url)
        query = parse_qs(url.query)
        target = "/time-attendance/contracts" if accepts else "/login"
        if "submit" in query:
            submissions.append({"email": query["email"][0], "pw": query["pw"][0]})
            route.fulfill(**page(DEEL_TRACKING if accepts else DEEL_LOGIN.format(prefill="", target=target)))
        elif url.path == "/login":
            route.fulfill(**page(DEEL_LOGIN.format(prefill=prefill, target=target)))
        else:  # not signed in: Deel sends you to the login page (a script, since Chromium follows a fulfilled
            # 302 over the real network instead of through this handler)
            route.fulfill(**page("<!doctype html><script>location.replace('/login')</script>"))

    session.context.route("https://app.deel.com/**", handle)
    return submissions


def test_deel_login_types_the_env_details_once(make_settings):
    settings = make_settings(HEADLESS="true", LOGIN_TIMEOUT_SECONDS="30", GLOBALSOURCE_EMAIL="me@example.com",
                             GLOBALSOURCE_PASSWORD="fake-password-1",
                             GLOBALSOURCE_URL="https://app.deel.com/time-attendance/contracts")
    platform = make_platform(settings, "globalsource", {})
    with BrowserPool(settings) as pool:
        platform.session = session = pool.open(platform)
        submissions = fake_deel(session, accepts=True, prefill="someone-else@example.com")
        platform.login()  # lands on Time tracking only if the form was filled and submitted
        session.close()
    assert submissions == [{"email": "me@example.com", "pw": "fake-password-1"}]  # wrong prefilled email replaced


def test_deel_login_does_not_retry_a_refused_password(make_settings):
    settings = make_settings(HEADLESS="true", LOGIN_TIMEOUT_SECONDS="6", GLOBALSOURCE_EMAIL="me@example.com",
                             GLOBALSOURCE_PASSWORD="fake-password-2",
                             GLOBALSOURCE_URL="https://app.deel.com/time-attendance/contracts")
    platform = make_platform(settings, "globalsource", {})
    with BrowserPool(settings) as pool:
        platform.session = session = pool.open(platform)
        submissions = fake_deel(session, accepts=False)
        with pytest.raises(PlatformError, match="not signed in to Deel in time"):
            platform.login()
        session.close()
    assert len(submissions) == 1  # typed once; after a refusal the person takes over


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
