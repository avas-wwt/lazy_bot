from datetime import date

import pytest

from lazy_bot.config import ConfigError
from lazy_bot.platforms.kronos import NOTE_LIMIT, activity_query, comment_lines, kronos_note


def test_notes_swap_the_characters_kronos_refuses():
    assert kronos_note("Citadel - EVRA/Patching Automation/WWT") == "Citadel - EVRA-Patching Automation-WWT"
    assert kronos_note(r"a\b|c <d> [e]") == "a-b-c (d) (e)"
    assert kronos_note("Cloud PC & Access: ação, café (ok)!") == "Cloud PC & Access: ação, café (ok)!"  # kept as is
    assert len(kronos_note("x" * 300)) == NOTE_LIMIT


def test_a_long_note_is_cut_after_the_last_whole_item():
    thursday = ("EPM-351: sent inventory export requests to the source owners (ServiceNow, SCCM/Foreman/Automox, "
                "vCenters, cloud, scanners) and followed up on replies from David Duenes, Philip Davis and William "
                "Hower, profiled the ServiceNow CMDB server table (91,659 records, 68% retired, 28,555 installed), "
                "built the source catalog, reconciliation workbook and request tracker, INTERNAL - JHA Daily "
                "Stand-up, Citadel - Enterprise Patching")
    assert kronos_note(thursday).endswith("from David Duenes, Philip Davis and William Hower")  # not "(91,659"
    assert kronos_note("items, (" + "detail, " * 40 + "end)") == "items"  # never inside brackets
    words = kronos_note("word " * 60)
    assert words.endswith("word") and len(words) <= NOTE_LIMIT


def test_a_long_note_written_in_sentences_keeps_its_last_whole_sentence():
    monday = ("Finalized the EPM-349 service-account request process for review. With no EPM-348 notes available, "
              "the open decisions are listed with a proposed default for each. Prepared backlog stories for sprint "
              "planning at Bryan's request. Analyzed Automox devices with no recent check-in (398 cases, most of "
              "them alive in CrowdStrike) and drafted the Automox decommission story Sam asked for.")
    assert kronos_note(monday).endswith("for sprint planning at Bryan's request.")  # not "...notes available"
    tuesday = ("Kept troubleshooting Revelio write access after it was granted (HTTPS and SSH both returned 403 until "
               "the permission was fixed). Answered Bryan and Sim on Automox numbers, explaining the difference "
               "between agent deployment and patch compliance (48% compliant after policies started).")
    assert kronos_note(tuesday).endswith("). Answered Bryan and Sim on Automox numbers")  # a later item still wins
    assert kronos_note("Coverage went from 47.1 to 96.9 " + "percent " * 40).startswith("Coverage went from 47.1 to")


def test_comment_lines_read_the_comments_section_of_a_cell():
    empty_note = "Status\nComments [1]\nActivity Comment\nRecent history\nLast edit date:"
    with_note = "Comments [1]\nActivity Comment\nJHA Daily Stand-up\nRecent history"
    assert comment_lines(empty_note) == ["Activity Comment"]
    assert comment_lines(with_note) == ["Activity Comment", "JHA Daily Stand-up"]
    assert comment_lines("Comments [0]\nRecent history") == []
    assert comment_lines("no comments section here") == []


def test_billing_moves_to_a_new_code_from_a_monday():
    milestone = "10039405*; 2026-11-23: 10039404*"
    assert activity_query("10039405*", date(2026, 11, 23)) == "10039405*"
    assert activity_query(milestone, date(2026, 11, 16)) == "10039405*"  # the last week on the old code
    assert activity_query(milestone, date(2026, 11, 23)) == "10039404*"
    assert activity_query(milestone + "; 2027-01-04: 10039406*", date(2027, 1, 4)) == "10039406*"
    assert activity_query(milestone + ";", date(2026, 11, 30)) == "10039404*"  # a stray ; is fine


@pytest.mark.parametrize("setting, problem", [
    ("10039405*; 2026-11-25: 10039404*", "is a Wednesday"),
    ("10039405*; 23/11/2026: 10039404*", "should read"),
    ("10039405*; 2026-11-23:", "should read"),
    ("10039405*; 2026-11-30: 1*; 2026-11-23: 2*", "date order"),
    ("2026-11-23: 10039404*", "should start with"),
])
def test_a_code_change_that_cannot_be_read_is_explained(setting, problem):
    with pytest.raises(ConfigError, match=problem):
        activity_query(setting, date(2026, 11, 23))
