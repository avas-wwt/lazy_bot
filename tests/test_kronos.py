from lazy_bot.platforms.kronos import NOTE_LIMIT, comment_lines, kronos_note


def test_notes_swap_the_characters_kronos_refuses():
    assert kronos_note("Citadel - EVRA/Patching Automation/WWT") == "Citadel - EVRA-Patching Automation-WWT"
    assert kronos_note(r"a\b|c <d> [e]") == "a-b-c (d) (e)"
    assert kronos_note("Cloud PC & Access: ação, café (ok)!") == "Cloud PC & Access: ação, café (ok)!"  # kept as is
    assert len(kronos_note("x" * 300)) == NOTE_LIMIT


def test_comment_lines_read_the_comments_section_of_a_cell():
    empty_note = "Status\nComments [1]\nActivity Comment\nRecent history\nLast edit date:"
    with_note = "Comments [1]\nActivity Comment\nJHA Daily Stand-up\nRecent history"
    assert comment_lines(empty_note) == ["Activity Comment"]
    assert comment_lines(with_note) == ["Activity Comment", "JHA Daily Stand-up"]
    assert comment_lines("Comments [0]\nRecent history") == []
    assert comment_lines("no comments section here") == []
