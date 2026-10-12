import json
import logging
from datetime import date, datetime

import pytest

from lazy_bot.config import ConfigError
from lazy_bot.platforms.base import PlatformError
from lazy_bot.platforms.clockify import DEFAULT_API, ClockifyPlatform, local_day, utc_text
from lazy_bot.timesheet import read_timesheet


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data
        self.ok = 200 <= status < 300
        self.content = b"" if data is None else json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._data


class FakeClockify:
    """Just enough of the Clockify API for the adapter, kept in memory."""

    def __init__(self, *, force_projects=False, limit=None):
        self.entries: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.force_projects = force_projects
        self.limit = limit
        self._next_id = 1

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        assert headers == {"X-Api-Key": "test-key"}
        path = url.removeprefix(DEFAULT_API)
        self.calls.append((method, path))
        if self.limit is not None and len(self.calls) > self.limit:
            return Response(429, {"message": "Too many requests"})
        if (method, path) == ("GET", "/user"):
            return Response(data={"id": "u1", "name": "Pat", "activeWorkspace": "w1", "defaultWorkspace": "w1"})
        if (method, path) == ("GET", "/workspaces/w1"):
            return Response(data={"id": "w1", "name": "Acme",
                                  "workspaceSettings": {"forceProjects": self.force_projects}})
        if (method, path) == ("GET", "/workspaces/w1/projects"):
            return Response(data=[{"id": "p1", "name": "WWT Delivery"}, {"id": "p2", "name": "WWT Delivery (old)"}])
        if (method, path) == ("GET", f"/workspaces/w1/projects/{PROJECT_ID}"):
            return Response(data={"id": PROJECT_ID, "name": "JH20260921-Patch Mgmt - Alfeu"})
        if (method, path) == ("GET", "/workspaces/w1/user/u1/time-entries"):
            return Response(data=[e for e in self.entries.values()
                                  if params["start"] <= e["timeInterval"]["start"] < params["end"]])
        if (method, path) == ("POST", "/workspaces/w1/time-entries"):
            entry_id = f"e{self._next_id}"
            self._next_id += 1
            return Response(201, self._store(entry_id, json))
        if path.startswith("/workspaces/w1/time-entries/"):
            entry_id = path.rsplit("/", 1)[1]
            if method == "DELETE":
                del self.entries[entry_id]
                return Response(204)
            if method == "PUT":
                return Response(data=self._store(entry_id, json))
        return Response(404, {"message": f"no route for {method} {path}"})

    def _store(self, entry_id, body):
        self.entries[entry_id] = {
            "id": entry_id, "description": body["description"], "projectId": body["projectId"],
            "taskId": body["taskId"], "tagIds": body["tagIds"],
            "timeInterval": {"start": body["start"], "end": body["end"]},
        }
        return self.entries[entry_id]

    def writes(self):
        return [call for call in self.calls if call[0] in ("POST", "PUT", "DELETE")]


PROJECT_ID = "6ac9332bbe2ed8690f5be4e2"
WEEK = """date,hours,description
2026-10-05,8,Sprint planning
2026-10-06,7.5,Code review
2026-10-07,8,API integration
2026-10-08,8,Bug fixes
2026-10-09,8,Demo prep
"""


@pytest.fixture
def clockify(make_settings):
    def make(api=None, cache=None, **values):
        settings = make_settings(**{"CLOCKIFY_API_KEY": "test-key", "CLOCKIFY_PROJECT": "WWT Delivery", **values})
        platform = ClockifyPlatform(settings.platform("clockify"), settings, {} if cache is None else cache)
        platform.http = api or FakeClockify()
        return platform

    return make


def week_of(write_csv, settings, text=WEEK):
    write_csv(text)
    return read_timesheet(settings.csv_path, settings.date_formats).weeks()[0]


def test_utc_round_trip():
    text = utc_text(datetime(2026, 10, 5, 9, 0))
    assert text.endswith("Z") and len(text) == 20
    assert local_day(text) == date(2026, 10, 5)
    assert datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().hour == 9


def test_login_looks_ids_up_once(clockify):
    cache = {}
    first = clockify(cache=cache)
    first.login()
    assert first.http.calls == [("GET", "/user"), ("GET", "/workspaces/w1"), ("GET", "/workspaces/w1/projects")]
    assert (cache["workspace_id"], cache["project_id"]) == ("w1", "p1")

    again = clockify(cache=cache)
    again.login()
    assert again.http.calls == []


@pytest.mark.parametrize("value", [PROJECT_ID, f"https://app.clockify.me/projects/{PROJECT_ID}/edit"])
def test_project_can_be_an_id_or_a_pasted_url(clockify, value):
    platform = clockify(CLOCKIFY_PROJECT=value)
    platform.login()
    assert platform.cache["project_id"] == PROJECT_ID
    assert ("GET", "/workspaces/w1/projects") not in platform.http.calls  # no name search needed
    assert platform.describe() == ("the API key works for Pat; workspace 'Acme'; entries go to project "
                                   "'JH20260921-Patch Mgmt - Alfeu'; the workspace requires no extra fields")


def test_unknown_project_id_is_explained(clockify):
    with pytest.raises(ConfigError, match="no project with id 0123456789abcdef01234567"):
        clockify(CLOCKIFY_PROJECT="0123456789abcdef01234567").login()


def test_required_project_and_unknown_project_are_explained(clockify):
    with pytest.raises(ConfigError, match="requires a project"):
        clockify(api=FakeClockify(force_projects=True), CLOCKIFY_PROJECT="").login()
    with pytest.raises(ConfigError, match="no project named 'Nope'"):
        clockify(CLOCKIFY_PROJECT="Nope").login()


def test_fill_creates_once_then_only_updates_what_changed(clockify, write_csv):
    platform = clockify()
    platform.login()
    week = week_of(write_csv, platform.app)
    store = {}
    assert platform.fill_week(week, store) == "entries saved (5 created)"
    tuesday = platform.http.entries[store["entries"]["2026-10-06"]]
    start = datetime.fromisoformat(tuesday["timeInterval"]["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(tuesday["timeInterval"]["end"].replace("Z", "+00:00"))
    assert start.astimezone().hour == 9  # CLOCKIFY_DAY_START, in local time
    assert (end - start).total_seconds() == 7.5 * 3600
    assert (tuesday["description"], tuesday["projectId"]) == ("Code review", "p1")

    platform.http.calls.clear()
    assert platform.fill_week(week, store) == "entries saved (5 unchanged)"
    assert platform.http.writes() == []

    edited = week_of(write_csv, platform.app, WEEK.replace("7.5,Code review", "6,Code review"))
    assert platform.fill_week(edited, store) == "entries saved (1 updated, 4 unchanged)"
    assert platform.http.writes() == [("PUT", f"/workspaces/w1/time-entries/{store['entries']['2026-10-06']}")]
    assert len(platform.http.entries) == 5


def test_days_with_entries_typed_by_hand_are_left_alone(clockify, write_csv, caplog):
    platform = clockify()
    platform.login()
    week = week_of(write_csv, platform.app)
    wednesday = utc_text(datetime(2026, 10, 7, 14, 0))
    platform.http.entries["manual"] = {"id": "manual", "description": "typed by hand", "projectId": "p1",
                                       "timeInterval": {"start": wednesday, "end": wednesday}}
    with caplog.at_level(logging.WARNING, logger="lazy_bot"):
        assert platform.fill_week(week, {}) == "entries saved (4 created)"
    assert "1 entry on 2026-10-07 that lazy_bot didn't create" in caplog.text


def test_a_day_removed_from_the_csv_is_removed_from_clockify(clockify, write_csv):
    platform = clockify()
    platform.login()
    store = {}
    platform.fill_week(week_of(write_csv, platform.app), store)
    friday = store["entries"]["2026-10-09"]

    shorter = week_of(write_csv, platform.app, WEEK.replace("2026-10-09,8,Demo prep\n", ""))
    assert platform.fill_week(shorter, store) == "entries saved (4 unchanged, 1 deleted)"
    assert friday not in platform.http.entries and "2026-10-09" not in store["entries"]


def test_rehearsal_writes_nothing(clockify, write_csv):
    platform = clockify()
    platform.login()
    platform.rehearsal = True
    store = {}
    assert platform.fill_week(week_of(write_csv, platform.app), store) == \
        "nothing written; a real run would do: 5 created"
    assert platform.http.writes() == [] and store == {"entries": {}}


def test_rate_limit_says_it_will_carry_on(clockify):
    with pytest.raises(PlatformError, match="30 an hour"):
        clockify(api=FakeClockify(limit=1)).login()


def test_submit_is_off_unless_asked_for(clockify):
    assert not clockify().has_submit()
    assert clockify(CLOCKIFY_SUBMIT_APPROVAL="true").has_submit()
