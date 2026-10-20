"""`submit` command paths that never reach a site."""

from lazy_bot.cli import main


def env_file(tmp_path, *lines: str) -> str:
    env = tmp_path / "submit.env"
    env.write_text("\n".join([
        f"STATE_PATH='{tmp_path / 'state.json'}'", f"PROFILES_DIR='{tmp_path / 'profiles'}'",
        f"LOGS_DIR='{tmp_path / 'logs'}'", "CLOCKIFY_API_KEY=test-key", *lines]), encoding="utf-8")
    return str(env)


def test_submit_does_nothing_when_the_settings_have_no_submit_step(tmp_path, capsys):
    env = env_file(tmp_path, "PLATFORMS=clockify", "CLOCKIFY_SUBMIT_APPROVAL=false")
    assert main(["--env-file", env, "submit", "--platform", "clockify", "--week", "2026-10-14"]) == 0
    assert "nothing to submit for the week of 2026-10-12" in capsys.readouterr().out


def test_submit_explains_sites_without_a_separate_submit(tmp_path, capsys):
    env = env_file(tmp_path, "PLATFORMS=globalsource")
    assert main(["--env-file", env, "submit", "--platform", "globalsource", "--week", "2026-10-14"]) == 1
    assert "sends each entry as it is written" in capsys.readouterr().out


def test_submit_asks_first_and_takes_no_answer_as_no(tmp_path, capsys):
    env = env_file(tmp_path, "PLATFORMS=clockify", "CLOCKIFY_SUBMIT_APPROVAL=true")
    assert main(["--env-file", env, "submit", "--platform", "clockify", "--week", "2026-10-14"]) == 0
    out = capsys.readouterr().out
    assert "[clockify] not submitted" in out
    assert not (tmp_path / "state.json").exists()  # nothing was attempted, nothing recorded
