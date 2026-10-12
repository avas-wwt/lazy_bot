"""Clockify, through its REST API (https://docs.clockify.me). No browser needed.

Each logged day becomes one entry starting at CLOCKIFY_DAY_START. The ids of the entries lazy_bot creates are
kept in state.json, so a re-run updates them instead of adding more. Days that already have entries lazy_bot
didn't create are left alone. Free workspaces allow 30 API requests an hour, so ids are cached and a week
takes about six requests.
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


class ClockifyPlatform(Platform):
    name = "clockify"
    title = "Clockify"
    uses_browser = False

    def __init__(self, settings, app, cache):
        super().__init__(settings, app, cache)
        self.http = requests.Session()

    def has_submit(self) -> bool:
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
            self.api, key, self.settings.get("WORKSPACE_ID"), self.settings.get("PROJECT"),
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
        self.cache.clear()
        self.cache.update(
            settings_id=settings_id, user_id=user["id"], user_name=user.get("name") or user.get("email", ""),
            workspace_id=workspace_id, workspace_name=workspace.get("name", workspace_id),
            project_id=project["id"] if project else None, project_name=project.get("name") if project else None,
            task_id=task["id"] if task else None, task_name=task.get("name") if task else None,
            tag_ids=[tag["id"] for tag in tags], required=[name for flag, name in _REQUIRED if rules.get(flag)],
            force_description=bool(rules.get("forceDescription")),
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
                    self._call("DELETE", f"/workspaces/{workspace}/time-entries/{ours}")
                    counts["deleted"] += 1
                mine.pop(key, None)
                continue
            if others:
                self.log.warning("! Clockify already has %d entr%s on %s that lazy_bot didn't create; left alone.",
                                 len(others), "y" if len(others) == 1 else "ies", day)
                continue
            body = self._body(entry)
            if ours is None:
                mine[key] = self._call("POST", f"/workspaces/{workspace}/time-entries", json=body)["id"]
                counts["created"] += 1
            elif self._same(by_id[ours], body):
                counts["unchanged"] += 1
            else:
                self._call("PUT", f"/workspaces/{workspace}/time-entries/{ours}", json=body)
                counts["updated"] += 1
        done = ", ".join(f"{n} {what}" for what, n in counts.items() if n)
        return f"entries saved ({done or 'nothing to do'})"

    def submit(self, week: Week, store: dict) -> str:
        self._call("POST", f"/workspaces/{self.cache['workspace_id']}/approval-requests/TIMESHEET",
                   json={"period": "WEEKLY", "periodStart": f"{week.start.isoformat()}T00:00:00Z"})
        return "sent for approval"

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
        if status in (401, 403):
            raise ClockifyHttpError("Clockify refused the API key; check CLOCKIFY_API_KEY "
                                    "(and CLOCKIFY_API_URL if your workspace is in another region)", status)
        if not response.ok:
            try:
                detail = response.json().get("message", "")
            except ValueError:
                detail = response.text[:200]
            raise ClockifyHttpError(f"Clockify {method} {path.split('?')[0]} failed: {status} {detail}".strip(), status)
        return response.json() if response.content else None
