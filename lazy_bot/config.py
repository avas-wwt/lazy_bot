"""Settings come from .env, so each person only edits their own copy."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"
DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
SUBMIT_MODES = ("confirm", "submit", "save")
ISO_DATE = "%Y-%m-%d"


class ConfigError(Exception):
    """A value in .env is missing or can't be used."""


def parse_bool(raw: str, key: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}={raw!r} should be true or false")


@dataclass(frozen=True)
class PlatformSettings:
    """One platform's `<NAME>_*` values from .env, keyed without the prefix."""

    name: str
    values: dict[str, str] = field(repr=False)

    def key(self, key: str) -> str:
        return f"{self.name.upper()}_{key.upper()}"

    def get(self, key: str, default: str = "", *, strip: bool = True) -> str:
        value = self.values.get(key.upper(), "")
        if strip:
            value = value.strip()
        return value or default

    def require(self, key: str) -> str:
        value = self.get(key)
        if not value:
            raise ConfigError(f"{self.key(key)} is empty in .env")
        return value

    def flag(self, key: str, default: bool = False) -> bool:
        raw = self.get(key)
        return parse_bool(raw, self.key(key)) if raw else default

    def number(self, key: str, default: float) -> float:
        raw = self.get(key)
        if not raw:
            return default
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            raise ConfigError(f"{self.key(key)}={raw!r} should be a number") from None


@dataclass(frozen=True)
class Settings:
    csv_path: Path
    date_formats: tuple[str, ...]
    platforms: tuple[str, ...]
    submit_mode: str
    run_day: int
    run_time: time
    login_timeout: float
    headless: bool
    profiles_dir: Path
    archive_dir: Path
    logs_dir: Path
    state_path: Path
    env: dict[str, str] = field(repr=False)  # never printed: it holds passwords and API keys

    def platform(self, name: str) -> PlatformSettings:
        prefix = f"{name.upper()}_"
        return PlatformSettings(name, {k[len(prefix):]: v for k, v in self.env.items() if k.startswith(prefix)})

    def profile_dir(self, name: str) -> Path:
        return self.profiles_dir / name


def _path(raw: str, default: Path) -> Path:
    if not raw:
        return default
    path = Path(os.path.expandvars(raw)).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def load_settings(env_file: Path | None = None, overrides: dict[str, str] | None = None) -> Settings:
    """Read .env (values stay literal: no $VAR expansion, so passwords are safe). `overrides` is for tests."""
    path = env_file or DEFAULT_ENV_FILE
    env: dict[str, str] = {}
    if path.exists():
        env = {k.upper(): v or "" for k, v in dotenv_values(path, interpolate=False).items()}
    elif overrides is None:
        raise ConfigError(f"{path} not found. Copy .env.example to .env and fill it in.")
    env.update({k.upper(): v for k, v in (overrides or {}).items()})

    def get(key: str, default: str = "") -> str:
        return env.get(key, "").strip() or default

    formats = [f.strip() for f in get("DATE_FORMATS", f"{ISO_DATE},%m/%d/%Y").split(",") if f.strip()]
    if ISO_DATE not in formats:
        formats.insert(0, ISO_DATE)

    mode = get("SUBMIT_MODE", "confirm").lower()
    if mode not in SUBMIT_MODES:
        raise ConfigError(f"SUBMIT_MODE={mode!r} should be one of: {', '.join(SUBMIT_MODES)}")

    day = get("RUN_DAY", "FRI").upper()[:3]
    if day not in DAY_NAMES:
        raise ConfigError(f"RUN_DAY={get('RUN_DAY')!r} should be a day name such as FRI")

    try:
        run_time = datetime.strptime(get("RUN_TIME", "16:00"), "%H:%M").time()
    except ValueError:
        raise ConfigError(f"RUN_TIME={get('RUN_TIME')!r} should look like 16:00") from None

    try:
        login_timeout = float(get("LOGIN_TIMEOUT_SECONDS", "300"))
    except ValueError:
        raise ConfigError(f"LOGIN_TIMEOUT_SECONDS={get('LOGIN_TIMEOUT_SECONDS')!r} should be a number") from None

    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Settings(
        csv_path=_path(get("CSV_PATH"), PROJECT_DIR / "timesheet.csv"),
        date_formats=tuple(formats),
        platforms=tuple(dict.fromkeys(p.strip().lower() for p in get("PLATFORMS").split(",") if p.strip())),
        submit_mode=mode,
        run_day=DAY_NAMES.index(day),
        run_time=run_time,
        login_timeout=login_timeout,
        headless=parse_bool(get("HEADLESS", "false"), "HEADLESS"),
        profiles_dir=_path(get("PROFILES_DIR"), local / "lazy_bot" / "profiles"),
        archive_dir=_path(get("ARCHIVE_DIR"), PROJECT_DIR / "archive"),
        logs_dir=_path(get("LOGS_DIR"), PROJECT_DIR / "logs"),
        state_path=_path(get("STATE_PATH"), PROJECT_DIR / "state.json"),
        env=env,
    )
