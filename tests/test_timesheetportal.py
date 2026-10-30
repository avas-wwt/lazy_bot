from datetime import date

import pytest

from lazy_bot.platforms.base import PlatformError
from lazy_bot.platforms.timesheetportal import TimesheetPortalPlatform, clock, month_ends, timesheet_row
from lazy_bot.timesheet import Week


def week_of(monday: date) -> Week:
    return Week(monday, [], [])


def test_only_the_week_holding_a_month_end_submits(make_settings):
    platform = TimesheetPortalPlatform(make_settings().platform("timesheetportal"), make_settings(), {})
    assert month_ends(week_of(date(2026, 10, 12))) == []
    assert not platform.needs_submit(week_of(date(2026, 10, 12)))
    assert month_ends(week_of(date(2026, 10, 26))) == [date(2026, 10, 31)]  # Mon 26/10 to Sun 1/11
    assert platform.needs_submit(week_of(date(2026, 10, 26)))
    # November ends on Monday 30/11: it's submitted that week, once every November day is in
    assert month_ends(week_of(date(2026, 11, 23))) == []
    assert month_ends(week_of(date(2026, 11, 30))) == [date(2026, 11, 30)]


def test_week_across_two_months_is_split():
    months = TimesheetPortalPlatform._by_month(week_of(date(2026, 10, 26)))
    assert list(months) == [(2026, 10), (2026, 11)]
    assert len(months[(2026, 10)]) == 6 and months[(2026, 11)] == [date(2026, 11, 1)]


def test_hours_are_written_as_hh_mm():
    assert (clock(480), clock(450), clock(0), clock(1440)) == ("08:00", "07:30", "00:00", "24:00")


def test_hour_boxes_row_is_read_from_the_page():
    # October's boxes were ctlEnd_1_<day - 1>_0, November's ctlEnd_2_<day - 1>_0
    assert timesheet_row(["ctlEnd_2_0_0", "ctlEnd_2_29_0", "MainContent_btnNextWeek"]) == "2"
    for ids in ([], ["ctlEnd_1_0_0", "ctlEnd_2_0_0"]):
        with pytest.raises(PlatformError):
            timesheet_row(ids)
