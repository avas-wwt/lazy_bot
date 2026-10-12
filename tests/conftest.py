from pathlib import Path

import pytest

from lazy_bot.config import Settings, load_settings

WEEK_CSV = """date,hours,description
2026-10-05,8,Sprint planning
2026-10-06,7.5,Code review
2026-10-07,8,API integration
2026-10-08,8,Bug fixes
2026-10-09,8,Demo prep
2026-10-12,4,Next week already
"""


@pytest.fixture
def make_settings(tmp_path: Path):
    """Settings that only ever touch tmp_path (never the real .env, CSV or profiles)."""

    def make(**values: object) -> Settings:
        base = {
            "CSV_PATH": str(tmp_path / "timesheet.csv"),
            "ARCHIVE_DIR": str(tmp_path / "archive"),
            "LOGS_DIR": str(tmp_path / "logs"),
            "STATE_PATH": str(tmp_path / "state.json"),
            "PROFILES_DIR": str(tmp_path / "profiles"),
        }
        base.update({key.upper(): str(value) for key, value in values.items()})
        return load_settings(tmp_path / "missing.env", overrides=base)

    return make


@pytest.fixture
def write_csv(tmp_path: Path):
    def write(text: str = WEEK_CSV, encoding: str = "utf-8", name: str = "timesheet.csv") -> Path:
        path = tmp_path / name
        path.write_bytes(text.encode(encoding))
        return path

    return write
