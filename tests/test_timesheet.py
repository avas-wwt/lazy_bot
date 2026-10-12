import sys
from datetime import date, datetime, time

import pytest

from lazy_bot.timesheet import FileLockedError, TimesheetError, parse_minutes, read_timesheet

FORMATS = ("%Y-%m-%d", "%m/%d/%Y")


def test_reads_rows_and_merges_a_day_logged_twice(write_csv):
    path = write_csv("date,hours,description\n"
                     "2026-10-05,6,Planning\n"
                     "2026-10-05,2,Support call\n"
                     "2026-10-06,7.5,Review\n")
    week = read_timesheet(path, FORMATS).weeks()[0]
    monday = week.entry(date(2026, 10, 5))
    assert (monday.minutes, monday.description) == (480, "Planning; Support call")
    assert week.hours_text() == "15.5"


def test_reads_excel_csv_with_semicolons_bom_and_decimal_commas(write_csv):
    path = write_csv("﻿Date;Hours;Description\n05/10/2026;7,5;Reunião de planejamento\n",
                     name="excel.csv")
    timesheet = read_timesheet(path, ("%Y-%m-%d", "%d/%m/%Y"))
    assert (timesheet.delimiter, timesheet.encoding) == (";", "utf-8-sig")
    entry = timesheet.weeks()[0].days[0]
    assert (entry.day, entry.minutes, entry.description) == (date(2026, 10, 5), 450, "Reunião de planejamento")


def test_reads_us_excel_dates_and_cp1252(write_csv):
    path = write_csv("date,hours,description\n10/12/2026,7:30,Café com o time\n", encoding="cp1252")
    timesheet = read_timesheet(path, FORMATS)
    assert timesheet.encoding == "cp1252"
    assert timesheet.weeks()[0].days[0].day == date(2026, 10, 12)
    assert timesheet.weeks()[0].days[0].description == "Café com o time"


def test_unquoted_comma_stays_in_the_description(write_csv):
    path = write_csv("date,hours,description\n2026-10-05,8,Planning, then reviews\n")
    assert read_timesheet(path, FORMATS).weeks()[0].days[0].description == "Planning, then reviews"


@pytest.mark.parametrize("text,minutes", [("8", 480), ("7.5", 450), ("7,5", 450), ("7:30", 450), ("0", 0), ("7.33", 440)])
def test_hours_formats(text, minutes):
    assert parse_minutes(text) == minutes


def test_lists_every_bad_line_at_once(write_csv):
    path = write_csv("date,hours,description\n"
                     "2026-13-40,8,Bad date\n"
                     "2026-10-06,eight,Bad hours\n"
                     "2026-10-07,20,Long\n"
                     "2026-10-07,5,Too long in total\n")
    with pytest.raises(TimesheetError) as error:
        read_timesheet(path, FORMATS)
    problems = "\n".join(error.value.problems)
    assert "line 2: can't read the date '2026-13-40'" in problems
    assert "line 3: can't read the hours 'eight'" in problems
    assert "2026-10-07: 25 h in one day" in problems


def test_header_must_name_the_columns(write_csv):
    path = write_csv("day,time,notes\n2026-10-05,8,x\n")
    with pytest.raises(TimesheetError, match="missing: date, hours, description"):
        read_timesheet(path, FORMATS)


def test_missing_description_is_only_a_warning(write_csv):
    timesheet = read_timesheet(write_csv("date,hours,description\n2026-10-05,8,\n"), FORMATS)
    assert timesheet.warnings == ["line 2: 2026-10-05 has no description"]


def test_weeks_missing_days_and_due_time(write_csv):
    weeks = read_timesheet(write_csv(), FORMATS).weeks()
    assert [w.key for w in weeks] == ["2026-10-05", "2026-10-12"]
    this_week, next_week = weeks
    assert this_week.missing_weekdays() == []
    assert next_week.missing_weekdays() == [date(2026, 10, d) for d in (13, 14, 15, 16)]
    due = this_week.due_at(run_day=4, run_time=time(16, 0))
    assert due == datetime(2026, 10, 9, 16, 0)


WEEK = """date,hours,description
2026-10-05,8,Sprint planning
2026-10-06,7.5,Code review
2026-10-12,4,Next week already
"""


def test_hash_follows_the_data(write_csv):
    def first_week_hash(text: str) -> str:
        return read_timesheet(write_csv(text), FORMATS).weeks()[0].data_hash()

    original = first_week_hash(WEEK)
    assert first_week_hash(WEEK) == original
    assert first_week_hash(WEEK.replace("7.5,Code review", "7,Code review")) != original
    assert first_week_hash(WEEK.replace("Code review", "Code reviews")) != original
    assert first_week_hash(WEEK.replace("2026-10-12,4", "2026-10-12,6")) == original  # other week


def test_archive_then_clear_keeps_other_weeks_and_the_file_format(write_csv, tmp_path):
    path = write_csv("﻿date;hours;description;extra\n2026-10-05;8;Planning;x\n2026-10-12;4;Later;y\n")
    timesheet = read_timesheet(path, FORMATS)
    week = timesheet.weeks()[0]

    archive = timesheet.write_archive(week, tmp_path / "archive")
    assert archive.name == "2026-10-05_to_2026-10-11.csv"
    assert archive.read_bytes() == "﻿date;hours;description;extra\r\n2026-10-05;8;Planning;x\r\n".encode("utf-8")
    assert timesheet.write_archive(week, tmp_path / "archive").name == "2026-10-05_to_2026-10-11_2.csv"

    assert timesheet.remove_week(week) == 1
    assert path.read_bytes() == "﻿date;hours;description;extra\r\n2026-10-12;4;Later;y\r\n".encode("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_clearing_a_file_open_elsewhere_raises(write_csv):
    path = write_csv()
    timesheet = read_timesheet(path, FORMATS)
    with open(path, "rb"):  # like Excel holding the file open
        with pytest.raises(FileLockedError):
            timesheet.remove_week(timesheet.weeks()[0])
    assert path.read_text(encoding="utf-8").count("\n") == 7  # untouched
