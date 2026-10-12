import logging
import sys

import pytest

from lazy_bot.platforms.base import Platform, PlatformError
from lazy_bot.runner import Runner
from lazy_bot.state import SAVED, SUBMITTED, State
from lazy_bot.timesheet import read_timesheet

EDITED_CSV = """date,hours,description
2026-10-05,8,Sprint planning
2026-10-06,6,Code review (left early)
2026-10-07,8,API integration
2026-10-08,8,Bug fixes
2026-10-09,8,Demo prep
2026-10-12,4,Next week already
"""


class Site:
    """Remembers what the fake platforms were asked to do."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.failing: set[tuple[str, str]] = set()


def platform_class(name: str, *, draft: bool = True, submit: bool = True) -> type[Platform]:
    class FakeApi(Platform):
        uses_browser = False
        has_draft = draft
        site: Site

        def has_submit(self) -> bool:
            return submit

        def _do(self, action: str, store: dict) -> str:
            if (self.name, action) in self.site.failing:
                raise PlatformError(f"{action} broke")
            self.site.calls.append((self.name, action))
            store[action] = store.get(action, 0) + 1
            return f"{action} ok"

        def login(self) -> None:
            self.site.calls.append((self.name, "login"))

        def fill_week(self, week, store) -> str:
            return self._do("fill", store)

        def submit(self, week, store) -> str:
            return self._do("submit", store)

    FakeApi.name = name
    FakeApi.title = name.title()
    return FakeApi


@pytest.fixture
def site() -> Site:
    return Site()


@pytest.fixture
def setup(make_settings, write_csv, site):
    """alpha: drafts + submit button, beta: drafts and nothing to submit, gamma: no drafts (like Deel)."""
    write_csv()
    settings = make_settings(PLATFORMS="alpha,beta,gamma")
    classes = {
        "alpha": platform_class("alpha"),
        "beta": platform_class("beta", submit=False),
        "gamma": platform_class("gamma", draft=False),
    }

    def runner(names=("alpha", "beta", "gamma"), *, mode="confirm", answer=True, only=None, force=False,
               retry=lambda message: False):
        state = State.load(settings.state_path)
        timesheet = read_timesheet(settings.csv_path, settings.date_formats)
        platforms = []
        for name in names:
            platform = classes[name](settings.platform(name), settings, state.cache(name))
            platform.site = site
            platforms.append(platform)
        asked = []

        def confirm(question, pump):
            asked.append(question)
            return answer

        run = Runner(settings, state, timesheet, platforms, mode=mode, run_dir=settings.logs_dir / "run",
                     only=only, force=force, confirm=confirm, retry_locked=retry)
        run.asked = asked
        return run, timesheet.weeks()[0]

    return settings, runner


def statuses(settings, week="2026-10-05"):
    data = State.load(settings.state_path).data["weeks"].get(week, {}).get("platforms", {})
    return {name: record.get("status") for name, record in data.items()}


def csv_dates(settings):
    return [line.split(",")[0] for line in settings.csv_path.read_text(encoding="utf-8").splitlines()]


def test_yes_fills_submits_and_archives(setup, site):
    settings, make = setup
    runner, week = make()
    assert runner.run_week(week)

    assert site.calls == [("alpha", "login"), ("alpha", "fill"), ("beta", "login"), ("beta", "fill"),
                          ("alpha", "submit"), ("gamma", "login"), ("gamma", "fill")]
    assert len(runner.asked) == 1
    assert statuses(settings) == {"alpha": SUBMITTED, "beta": SAVED, "gamma": SUBMITTED}
    archive = settings.archive_dir / "2026-10-05_to_2026-10-11.csv"
    assert archive.read_text(encoding="utf-8").count("\n") == 6  # header + 5 days
    assert csv_dates(settings) == ["date", "2026-10-12"]


def test_no_keeps_drafts_and_never_writes_to_sites_without_drafts(setup, site):
    settings, make = setup
    before = settings.csv_path.read_bytes()
    runner, week = make(answer=False)
    assert runner.run_week(week)

    assert ("gamma", "fill") not in site.calls
    assert ("alpha", "submit") not in site.calls
    assert statuses(settings) == {"alpha": SAVED, "beta": SAVED, "gamma": None}
    assert settings.csv_path.read_bytes() == before
    assert not settings.archive_dir.exists()


def test_failure_is_isolated_and_the_next_run_only_finishes_what_is_left(setup, site):
    settings, make = setup
    site.failing.add(("alpha", "fill"))
    runner, week = make()
    assert not runner.run_week(week)
    assert statuses(settings) == {"alpha": None, "beta": SAVED, "gamma": SUBMITTED}
    assert not settings.archive_dir.exists()

    site.failing.clear()
    site.calls.clear()
    runner, week = make()
    assert runner.run_week(week)
    assert site.calls == [("alpha", "login"), ("alpha", "fill"), ("alpha", "submit")]
    assert csv_dates(settings) == ["date", "2026-10-12"]


def test_edited_week_refills_saved_drafts(setup, site, write_csv):
    settings, make = setup
    runner, week = make(answer=False)
    runner.run_week(week)
    site.calls.clear()

    runner, week = make(answer=False)
    runner.run_week(week)
    assert ("alpha", "fill") not in site.calls  # same data: nothing to redo

    write_csv(EDITED_CSV)
    runner, week = make(answer=False)
    runner.run_week(week)
    assert ("alpha", "fill") in site.calls and ("beta", "fill") in site.calls


def test_week_changed_after_submit_is_left_alone_with_a_warning(setup, site, write_csv, caplog):
    settings, make = setup
    runner, week = make(names=("alpha", "beta"), only={"alpha"}, mode="submit")
    runner.run_week(week)  # alpha submitted; beta hasn't run, so the week stays in the CSV
    write_csv(EDITED_CSV)
    site.calls.clear()

    runner, week = make(names=("alpha", "beta"), only={"alpha"}, mode="submit")
    with caplog.at_level(logging.WARNING, logger="lazy_bot"):
        runner.run_week(week)
    assert site.calls == []
    assert "changed after it was submitted" in caplog.text


def test_save_mode_never_submits_and_skips_sites_without_drafts(setup, site):
    settings, make = setup
    runner, week = make(mode="save")
    assert runner.run_week(week)
    assert runner.asked == []
    assert ("alpha", "submit") not in site.calls and ("gamma", "fill") not in site.calls
    assert not settings.archive_dir.exists()  # gamma isn't done, so the week stays

    runner, week = make(names=("alpha", "beta"), mode="save")
    assert runner.run_week(week)
    assert csv_dates(settings) == ["date", "2026-10-12"]  # only draft sites: saved counts as done


def test_one_platform_at_a_time_archives_after_the_last_one(setup, site):
    settings, make = setup
    runner, week = make(names=("alpha", "beta"), only={"alpha"}, mode="submit")
    runner.run_week(week)
    assert statuses(settings) == {"alpha": SUBMITTED, "beta": None}
    assert len(csv_dates(settings)) == 7

    runner, week = make(names=("alpha", "beta"), only={"beta"}, mode="submit")
    runner.run_week(week)
    assert csv_dates(settings) == ["date", "2026-10-12"]


def test_force_redoes_finished_platforms(setup, site):
    settings, make = setup
    runner, week = make(names=("beta",))
    runner.run_week(week)  # beta saved -> week archived and cleared
    archive = settings.archive_dir / "2026-10-05_to_2026-10-11.csv"
    settings.csv_path.write_bytes(archive.read_bytes())  # put the week back
    site.calls.clear()

    runner, week = make(names=("beta",), force=True)
    runner.run_week(week)
    assert ("beta", "fill") in site.calls


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_locked_csv_is_cleared_on_the_next_run(setup, site):
    settings, make = setup
    runner, week = make(names=("beta",))
    with open(settings.csv_path, "rb"):  # like Excel holding the file open
        assert not runner.run_week(week)
    assert len(list(settings.archive_dir.iterdir())) == 1
    assert len(csv_dates(settings)) == 7

    runner, week = make(names=("beta",))
    assert runner.run_week(week)
    assert len(list(settings.archive_dir.iterdir())) == 1  # no second copy of the same data
    assert csv_dates(settings) == ["date", "2026-10-12"]
