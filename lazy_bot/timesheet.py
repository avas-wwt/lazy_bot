"""The daily log: read and check timesheet.csv, group it into weeks, archive finished weeks."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

COLUMNS = ("date", "hours", "description")
HEADER_LINE = ",".join(COLUMNS)
MAX_DAY_MINUTES = 24 * 60
_DECIMAL = re.compile(r"\d+(?:[.,]\d+)?")
_CLOCK = re.compile(r"(\d+):([0-5]\d)")


class TimesheetError(Exception):
    """The CSV can't be used. `problems` lists every bad line so they can all be fixed at once."""

    def __init__(self, path: Path, problems: list[str]):
        self.path = path
        self.problems = problems
        details = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"{path.name} needs fixing:\n{details}")


class FileLockedError(Exception):
    """The CSV is open in another program (usually Excel), so it can't be rewritten."""


def week_start(day: date) -> date:
    """The Monday of the week that contains `day`."""
    return day - timedelta(days=day.weekday())


def format_hours(minutes: int) -> str:
    """480 -> '8', 450 -> '7.5', 440 -> '7.33'."""
    return f"{minutes / 60:.2f}".rstrip("0").rstrip(".")


def parse_minutes(text: str) -> int:
    """'7.5', '7,5' or '7:30' -> 450."""
    value = text.strip()
    if _DECIMAL.fullmatch(value):
        return round(float(value.replace(",", ".")) * 60)
    if match := _CLOCK.fullmatch(value):
        return int(match[1]) * 60 + int(match[2])
    raise ValueError(f"not an amount of hours: {text!r}")


def parse_day(text: str, formats: Sequence[str]) -> date:
    for fmt in formats:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"not a date: {text!r}")


@dataclass(frozen=True)
class Row:
    """One CSV line. `cells` keeps every column as written, for the archive and the rewrite."""

    line: int
    cells: tuple[str, ...]
    day: date
    minutes: int
    description: str


@dataclass(frozen=True)
class DayEntry:
    """Everything logged for one date (several CSV rows on the same date are added up)."""

    day: date
    minutes: int
    description: str

    @property
    def hours(self) -> float:
        return self.minutes / 60

    def hours_text(self) -> str:
        return format_hours(self.minutes)

    def clock_text(self) -> str:
        """450 minutes -> '7:30'."""
        return f"{self.minutes // 60}:{self.minutes % 60:02d}"


@dataclass
class Week:
    """A Monday-to-Sunday week of entries."""

    start: date
    days: list[DayEntry]
    rows: list[Row]

    @property
    def end(self) -> date:
        return self.start + timedelta(days=6)

    @property
    def key(self) -> str:
        return self.start.isoformat()

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}_to_{self.end.isoformat()}"

    @property
    def total_minutes(self) -> int:
        return sum(d.minutes for d in self.days)

    def hours_text(self) -> str:
        return format_hours(self.total_minutes)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(7)]

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def entry(self, day: date) -> DayEntry | None:
        return next((d for d in self.days if d.day == day), None)

    def worked_days(self) -> list[DayEntry]:
        return [d for d in self.days if d.minutes > 0]

    def missing_weekdays(self) -> list[date]:
        logged = {d.day for d in self.days}
        return [day for day in self.dates()[:5] if day not in logged]

    def due_at(self, run_day: int, run_time: time) -> datetime:
        """When the scheduled run should pick this week up (e.g. its Friday at 16:00)."""
        return datetime.combine(self.start + timedelta(days=run_day), run_time)

    def data_hash(self) -> str:
        """Changes whenever the week's hours or descriptions change, so stale drafts get refilled."""
        text = "\n".join(f"{d.day}|{d.minutes}|{d.description}" for d in self.days)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class TimesheetFile:
    path: Path
    header: list[str]
    rows: list[Row]
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    warnings: list[str] = field(default_factory=list)

    def weeks(self) -> list[Week]:
        by_week: dict[date, list[Row]] = {}
        for row in sorted(self.rows, key=lambda r: (r.day, r.line)):
            by_week.setdefault(week_start(row.day), []).append(row)
        return [Week(start, _merge_days(rows), rows) for start, rows in sorted(by_week.items())]

    def write_archive(self, week: Week, archive_dir: Path) -> Path:
        """Copy the week's rows to archive/<monday>_to_<sunday>.csv (never overwrites an older copy)."""
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / f"{week.label}.csv"
        copy = 2
        while target.exists():
            target = archive_dir / f"{week.label}_{copy}.csv"
            copy += 1
        target.write_bytes(self._render(week.rows))
        return target

    def remove_week(self, week: Week) -> int:
        """Rewrite the CSV without the week's rows; the header and other weeks stay."""
        keep = [row for row in self.rows if not week.contains(row.day)]
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_bytes(self._render(keep))
        try:
            os.replace(temp, self.path)
        except PermissionError as exc:
            temp.unlink(missing_ok=True)
            raise FileLockedError(f"{self.path.name} is open in another program") from exc
        removed = len(self.rows) - len(keep)
        self.rows = keep
        return removed

    def _render(self, rows: Iterable[Row]) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=self.delimiter, lineterminator="\r\n")
        writer.writerow(self.header)
        writer.writerows(row.cells for row in rows)
        return buffer.getvalue().encode(self.encoding)


def _merge_days(rows: list[Row]) -> list[DayEntry]:
    merged: dict[date, tuple[int, list[str]]] = {}
    for row in rows:
        minutes, descriptions = merged.get(row.day, (0, []))
        merged[row.day] = (minutes + row.minutes, descriptions + ([row.description] if row.description else []))
    return [DayEntry(day, minutes, "; ".join(parts)) for day, (minutes, parts) in sorted(merged.items())]


def _decode(raw: bytes) -> tuple[str, str]:
    """Excel writes UTF-8 with a BOM ("CSV UTF-8") or the Windows code page; accept both."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


def read_timesheet(path: Path, date_formats: Sequence[str]) -> TimesheetFile:
    if not path.exists():
        raise TimesheetError(path, [f"file not found; create it with this first line: {HEADER_LINE}"])
    text, encoding = _decode(path.read_bytes())
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    examples = " or ".join(dict.fromkeys(date.today().strftime(f) for f in date_formats))

    header: list[str] | None = None
    index: dict[str, int] = {}
    rows: list[Row] = []
    problems: list[str] = []
    warnings: list[str] = []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue
        if header is None:
            names = [cell.strip().lower() for cell in cells]
            missing = [column for column in COLUMNS if column not in names]
            if missing:
                raise TimesheetError(path, [f"line {reader.line_num}: the first line must name the columns "
                                            f"{', '.join(COLUMNS)} (missing: {', '.join(missing)})"])
            header = cells
            index = {column: names.index(column) for column in COLUMNS}
            continue

        line = reader.line_num
        width = len(header)
        if len(cells) > width and index["description"] == width - 1:
            # An unquoted comma in the description split it; glue it back together.
            cells = cells[: width - 1] + [delimiter.join(cells[width - 1 :])]
        cells = cells + [""] * (width - len(cells))
        date_text, hours_text, description = (cells[index[column]].strip() for column in COLUMNS)
        try:
            day = parse_day(date_text, date_formats)
        except ValueError:
            problems.append(f"line {line}: can't read the date {date_text!r} (write it like {examples})")
            continue
        try:
            minutes = parse_minutes(hours_text)
        except ValueError:
            problems.append(f"line {line}: can't read the hours {hours_text!r} (write it like 8, 7.5, 7,5 or 7:30)")
            continue
        if not description:
            warnings.append(f"line {line}: {day} has no description")
        rows.append(Row(line, tuple(cells), day, minutes, description))

    if header is None:
        raise TimesheetError(path, [f"the file is empty; its first line must be: {HEADER_LINE}"])
    timesheet = TimesheetFile(path, header, rows, delimiter, encoding, warnings)
    for week in timesheet.weeks():
        for entry in week.days:
            if entry.minutes > MAX_DAY_MINUTES:
                problems.append(f"{entry.day}: {entry.hours_text()} h in one day (rows on the same date are added up)")
    if problems:
        raise TimesheetError(path, problems)
    return timesheet
