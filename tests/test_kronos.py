from lazy_bot.platforms.kronos import NOTE_LIMIT, comment_lines, kronos_note


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


def test_comment_lines_read_the_comments_section_of_a_cell():
    empty_note = "Status\nComments [1]\nActivity Comment\nRecent history\nLast edit date:"
    with_note = "Comments [1]\nActivity Comment\nJHA Daily Stand-up\nRecent history"
    assert comment_lines(empty_note) == ["Activity Comment"]
    assert comment_lines(with_note) == ["Activity Comment", "JHA Daily Stand-up"]
    assert comment_lines("Comments [0]\nRecent history") == []
    assert comment_lines("no comments section here") == []
