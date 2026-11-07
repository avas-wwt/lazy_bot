from datetime import date

from lazy_bot.platforms.globalsource import row_hours, row_label


def test_rows_are_named_like_deels_table():
    assert row_label(date(2026, 11, 2)) == "Mon Nov 2nd"
    assert row_label(date(2026, 11, 11)) == "Wed Nov 11th"


def test_row_hours_reads_what_a_day_already_has():
    assert row_hours("Tue Nov 3rd\tNo hours worked\tSubmit") is None
    assert row_hours("Mon Nov 2nd\tPublic holiday\tSubmit") is None  # a holiday label isn't hours
    assert row_hours("Fri Oct 30th\t09:00 - 13:00\t4 hrs\t4 hrs\tPending") == 4.0
    assert row_hours("Mon Nov 2nd\tPublic holiday\t8 hrs\tPending") == 8.0
    assert row_hours("Thu Nov 5th\tsomething new") == 0.0  # unknown: never overwritten
