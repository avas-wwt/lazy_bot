"""Clockify, through its REST API (https://docs.clockify.me). No browser needed.

Each logged day becomes one entry starting at CLOCKIFY_DAY_START. The ids of the entries lazy_bot creates are
kept in state.json, so a re-run updates them instead of adding more. Days that already have entries lazy_bot
didn't create are left alone. Free workspaces allow 30 API requests an hour, so ids are cached and a week
takes about six requests.

With CLOCKIFY_SUBMIT_APPROVAL=true, the week is then sent for approval after your "y". Clockify's approval
week starts on the day set in your Clockify profile (Sunday or Monday...), so lazy_bot sends the Clockify week
that contains its own Monday, then checks the dates Clockify answers with and withdraws the request at once
if they're not that week.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone

import requests

from ..config import ConfigError
from ..timesheet import DayEntry, Week
from .base import Platform, PlatformError

DEFAULT_API = "https://api.clockify.me/api/v1"
CACHE_VERSION = "2"  # bump when login() starts caching something new, so old caches refresh once
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
_ID = re.compile(r"[0-9a-f]{24}")
_ID_IN_URL = re.compile(r"projects/([0-9a-f]{24})")
_REQUIRED = (("forceProjects", "project"), ("forceTasks", "task"), ("forceTags", "tags"),
             ("forceDescription", "description"))


class ClockifyHttpError(PlatformError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def project_id_in(text: str) -> str | None:
    """'6ac9...' or a pasted https://app.clockify.me/projects/6ac9.../edit URL -> the id; a plain name -> None."""
    text = text.strip()
    if _ID.fullmatch(text):
        return text
    match = _ID_IN_URL.search(text)
    return match[1] if match else None


def utc_text(moment: datetime) -> str:
    """Clockify wants UTC with a Z; a naive datetime is taken as local time."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(text: str) -> date:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().date()


def possible_days(text: str) -> set[date]:
    """The day a Clockify timestamp means: Clockify writes period dates as midnight 'Z' in the owner's own
    time zone, so accept both readings (UTC date and local date)."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return {moment.date(), moment.astimezone().date()}


def approval_week(monday: date, week_start: str) -> tuple[date, date]:
    """The Clockify week (starting on `week_start`, e.g. 'SUNDAY') that contains lazy_bot's Monday."""
    first = monday - timedelta(days=(monday.weekday() - WEEKDAYS.index(week_start)) % 7)
    return first, first + timedelta(days=6)


class ClockifyPlatform(Platform):
    name = "clockify"
    title = "Clockify"
    uses_browser = False
    supports_rehearsal = True  # works out what it would write, and writes nothing

    def __init__(self, settings, app, cache):
        super().__init__(settings, app, cache)
        self.http = requests.Session()

    def needs_submit(self, week: Week) -> bool:
        return self.settings.flag("SUBMIT_APPROVAL")

    def submit_label(self) -> str:
        return "send the week for approval"

    @property
    def api(self) -> str:
        return self.settings.get("API_URL", DEFAULT_API).rstrip("/")

    # Sign-in: check the key and look up the ids once (cached until a related setting changes)

    def login(self) -> None:
        self._day_start(), self.settings.flag("BILLABLE")  # bad values fail here, before any entry is written
        key = self.settings.require("API_KEY")
        settings_id = hashlib.sha256("|".join([
            CACHE_VERSION, self.api, key, self.settings.get("WORKSPACE_ID"), self.settings.get("PROJECT"),
            self.settings.get("TASK"), self.settings.get("TAGS"),
        ]).encode()).hexdigest()[:16]
        if self.cache.get("settings_id") == settings_id:
            return
        user = self._call("GET", "/user")
        workspace_id = self.settings.get("WORKSPACE_ID") or user.get("activeWorkspace") or user.get("defaultWorkspace")
        workspace = self._call("GET", f"/workspaces/{workspace_id}") or {}
        rules = workspace.get("workspaceSettings", {})
        project = task = None
        if wanted := self.settings.get("PROJECT"):
            project = self._project(workspace_id, wanted)
            if task_name := self.settings.get("TASK"):
                task = self._find(f"/workspaces/{workspace_id}/projects/{project['id']}/tasks", task_name, "task",
                                  {"is-active": "true"})
        tags = [self._find(f"/workspaces/{workspace_id}/tags", tag.strip(), "tag", {"archived": "false"})
                for tag in self.settings.get("TAGS").split(",") if tag.strip()]
        if rules.get("forceProjects") and not project:
            raise ConfigError("this Clockify workspace requires a project: set CLOCKIFY_PROJECT in .env")
        if rules.get("forceTasks") and not task:
            raise ConfigError("this Clockify workspace requires a task: set CLOCKIFY_TASK in .env")
        if rules.get("forceTags") and not tags:
            raise ConfigError("this Clockify workspace requires tags: set CLOCKIFY_TAGS in .env")
        approvals = rules.get("approvalSettings") or {}
        week_start = str((user.get("settings") or {}).get("weekStart") or "MONDAY").upper()
        self.cache.clear()
        self.cache.update(
            settings_id=settings_id, user_id=user["id"], user_name=user.get("name") or user.get("email", ""),
            workspace_id=workspace_id, workspace_name=workspace.get("name", workspace_id),
            project_id=project["id"] if project else None, project_name=project.get("name") if project else None,
            task_id=task["id"] if task else None, task_name=task.get("name") if task else None,
            tag_ids=[tag["id"] for tag in tags], required=[name for flag, name in _REQUIRED if rules.get(flag)],
            force_description=bool(rules.get("forceDescription")),
            approval_enabled=bool(approvals.get("approvalEnabled", rules.get("timeApprovalEnabled"))),
            approval_period=approvals.get("approvalPeriod") or "WEEKLY",
            week_start=week_start if week_start in WEEKDAYS else "MONDAY",
        )

    def describe(self) -> str:
        cache = self.cache
        parts = [f"the API key works for {cache.get('user_name')}", f"workspace '{cache.get('workspace_name')}'"]
        if cache.get("project_name"):
            parts.append(f"entries go to project '{cache['project_name']}'")
        if cache.get("task_name"):
            parts.append(f"task '{cache['task_name']}'")
        parts.append(f"the workspace requires: {', '.join(cache['required'])}" if cache.get("required")
                     else "the workspace requires no extra fields")
        if cache.get("approval_enabled"):
            parts.append(f"timesheet approvals are {str(cache.get('approval_period')).lower().replace('_', '-')}, "
                         f"your Clockify weeks start on {str(cache.get('week_start')).title()}")
        return "; ".join(parts)

    def _project(self, workspace_id: str, wanted: str) -> dict:
        project_id = project_id_in(wanted)
        if project_id is None:
            return self._find(f"/workspaces/{workspace_id}/projects", wanted, "project", {"archived": "false"})
        try:
            return self._call("GET", f"/workspaces/{workspace_id}/projects/{project_id}")
        except ClockifyHttpError as exc:
            if exc.status in (400, 404):
                raise ConfigError(f"CLOCKIFY_PROJECT: this workspace has no project with id {project_id}") from None
            raise

    def _find(self, path: str, wanted: str, kind: str, params: dict) -> dict:
        items = self._call("GET", path, params={"name": wanted, "page-size": 200, **params}) or []
        for item in items:
            if item.get("name", "").strip().lower() == wanted.lower():
                return item
        names = ", ".join(sorted(item.get("name", "") for item in items)) or "none"
        raise ConfigError(f"Clockify has no {kind} named {wanted!r} (close matches: {names})")

    # The week

    def fill_week(self, week: Week, store: dict) -> str:
        if self.cache.get("force_description"):
            blank = [str(d.day) for d in week.worked_days() if not d.description]
            if blank:
                raise PlatformError(f"Clockify requires a description; add one for {', '.join(blank)}")
        workspace, user = self.cache["workspace_id"], self.cache["user_id"]
        mine: dict[str, str] = store.setdefault("entries", {})
        existing = self._call("GET", f"/workspaces/{workspace}/user/{user}/time-entries", params={
            "start": utc_text(datetime.combine(week.start, time())),
            "end": utc_text(datetime.combine(week.end + timedelta(days=1), time())),
            "page-size": 200,
        }) or []
        by_id = {entry["id"]: entry for entry in existing}
        counts = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        for day in week.dates():
            key = day.isoformat()
            ours = mine.get(key) if mine.get(key) in by_id else None
            others = [e for e in existing if e["id"] != ours and local_day(e["timeInterval"]["start"]) == day]
            entry = week.entry(day)
            if entry is None or entry.minutes == 0:
                if ours:
                    self._write("DELETE", f"/workspaces/{workspace}/time-entries/{ours}")
                    counts["deleted"] += 1
                mine.pop(key, None)
                continue
            if others:
                self.log.warning("! Clockify already has %d entr%s on %s that lazy_bot didn't create; left alone.",
                                 len(others), "y" if len(others) == 1 else "ies", day)
                continue
            body = self._body(entry)
            if ours is None:
                created = self._write("POST", f"/workspaces/{workspace}/time-entries", json=body)
                if created:
                    mine[key] = created["id"]
                counts["created"] += 1
            elif self._same(by_id[ours], body):
                counts["unchanged"] += 1
            else:
                self._write("PUT", f"/workspaces/{workspace}/time-entries/{ours}", json=body)
                counts["updated"] += 1
        done = ", ".join(f"{n} {what}" for what, n in counts.items() if n)
        if self.rehearsal:
            return f"nothing written; a real run would do: {done or 'nothing'}"
        return f"entries saved ({done or 'nothing to do'})"

    def _write(self, method: str, path: str, **kwargs):
        """POST/PUT/DELETE, or nothing at all while rehearsing."""
        return None if self.rehearsal else self._call(method, path, **kwargs)

    def submit(self, week: Week, store: dict) -> str:
        if not self.cache.get("approval_enabled"):
            return "nothing to send: this workspace doesn't use timesheet approvals"
        period = self.cache.get("approval_period") or "WEEKLY"
        if period != "WEEKLY":
            raise PlatformError(f"this workspace approves {period.lower().replace('_', '-')} periods; "
                                "lazy_bot only sends weekly approvals so far")
        first, last = approval_week(week.start, self.cache.get("week_start") or "MONDAY")
        span = f"{first:%a %d/%m} to {last:%a %d/%m}"
        if store.get("approval_withdrawn"):
            raise PlatformError(f"an earlier approval for {span} came back with the wrong dates and was withdrawn; "
                                "send this week for approval by hand in Clockify")
        workspace = self.cache["workspace_id"]
        try:
            created = self._call("POST", f"/workspaces/{workspace}/approval-requests/TIMESHEET",
                                 json={"period": "WEEKLY", "periodStart": f"{first.isoformat()}T00:00:00Z"}) or {}
        except ClockifyHttpError as exc:
            if exc.status in (400, 409) and re.search(r"already|exist", str(exc), re.I):
                return f"already sent for approval ({span})"
            raise
        request_id = created.get("id")
        if request_id:
            store["approval_request_id"] = request_id
        opened = (created.get("dateRange") or {}).get("start")
        if opened and first not in possible_days(opened):
            store["approval_withdrawn"] = True  # never retried automatically
            self._withdraw(workspace, request_id, opened, first)
        return f"sent for approval ({span})"

    def _withdraw(self, workspace: str, request_id: str | None, opened: str, wanted: date) -> None:
        """Clockify opened the wrong week: take the request back and say so."""
        problem = f"Clockify opened the approval for the week of {opened[:10]} instead of {wanted}"
        if not request_id:
            raise PlatformError(f"{problem}, and gave no id to withdraw it with: withdraw it in Clockify by hand")
        try:
            self._call("PATCH", f"/workspaces/{workspace}/approval-requests/{request_id}",
                       json={"state": "WITHDRAWN_SUBMISSION",
                             "note": "Sent for the wrong week by lazy_bot; withdrawn automatically."})
        except PlatformError as exc:
            raise PlatformError(f"{problem}, and lazy_bot couldn't withdraw it ({exc}): withdraw it in Clockify "
                                "by hand") from exc
        raise PlatformError(f"{problem}; it was withdrawn again. Send this week for approval by hand in Clockify")

    def _day_start(self) -> time:
        try:
            return datetime.strptime(self.settings.get("DAY_START", "09:00"), "%H:%M").time()
        except ValueError:
            raise ConfigError(f"CLOCKIFY_DAY_START={self.settings.get('DAY_START')!r} should look like 09:00") from None

    def _body(self, entry: DayEntry) -> dict:
        start = datetime.combine(entry.day, self._day_start())
        body = {
            "start": utc_text(start),
            "end": utc_text(start + timedelta(minutes=entry.minutes)),
            "description": entry.description,
            "projectId": self.cache.get("project_id"),
            "taskId": self.cache.get("task_id"),
            "tagIds": self.cache.get("tag_ids") or [],
        }
        if self.settings.get("BILLABLE"):
            body["billable"] = self.settings.flag("BILLABLE")
        return body

    @staticmethod
    def _same(current: dict, body: dict) -> bool:
        interval = current.get("timeInterval", {})
        return (interval.get("start"), interval.get("end"), current.get("description") or "",
                current.get("projectId"), current.get("taskId"), sorted(current.get("tagIds") or [])) == \
               (body["start"], body["end"], body["description"], body["projectId"], body["taskId"],
                sorted(body["tagIds"]))

    def _call(self, method: str, path: str, **kwargs):
        try:
            response = self.http.request(method, f"{self.api}{path}", timeout=30,
                                         headers={"X-Api-Key": self.settings.require("API_KEY")}, **kwargs)
        except requests.RequestException as exc:
            raise PlatformError(f"couldn't reach Clockify ({type(exc).__name__})") from exc
        status = response.status_code
        if status == 429:
            raise ClockifyHttpError("Clockify's request limit was reached (free workspaces allow 30 an hour); "
                                    "run again later and it will carry on", status)
        if status == 401:
            raise ClockifyHttpError("Clockify refused the API key; check CLOCKIFY_API_KEY "
                                    "(and CLOCKIFY_API_URL if your workspace is in another region)", status)
        if status == 403:
            raise ClockifyHttpError(f"Clockify doesn't let this account do {method} {path.split('?')[0]} (403)",
                                    status)
        if not response.ok:
            try:
                detail = response.json().get("message", "")
            except ValueError:
                detail = response.text[:200]
            raise ClockifyHttpError(f"Clockify {method} {path.split('?')[0]} failed: {status} {detail}".strip(), status)
        return response.json() if response.content else None
