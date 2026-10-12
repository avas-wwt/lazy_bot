from datetime import time

import pytest

from lazy_bot.config import ConfigError, load_settings


def test_defaults_and_platform_values(make_settings):
    settings = make_settings(PLATFORMS="Clockify, kronos,clockify", KRONOS_EMAIL="me@example.com")
    assert settings.platforms == ("clockify", "kronos")
    assert (settings.submit_mode, settings.run_day, settings.run_time) == ("confirm", 4, time(16, 0))
    assert settings.date_formats == ("%Y-%m-%d", "%m/%d/%Y")
    kronos = settings.platform("kronos")
    assert kronos.get("email") == "me@example.com"
    assert kronos.flag("APPROVE") is False
    assert "me@example.com" not in repr(settings)


def test_iso_dates_always_work(make_settings):
    assert make_settings(DATE_FORMATS="%d/%m/%Y").date_formats == ("%Y-%m-%d", "%d/%m/%Y")


@pytest.mark.parametrize("key,value", [("SUBMIT_MODE", "maybe"), ("RUN_DAY", "someday"), ("RUN_TIME", "4pm"),
                                       ("HEADLESS", "sometimes")])
def test_bad_values_are_explained(make_settings, key, value):
    with pytest.raises(ConfigError, match=key):
        make_settings(**{key: value})


def test_env_file_values_stay_literal(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PLATFORMS=fake\nFAKE_PASSWORD='p@ss #1 ${HOME}'\nCSV_PATH=data/log.csv\n", encoding="utf-8")
    settings = load_settings(env)
    assert settings.platform("fake").get("PASSWORD", strip=False) == "p@ss #1 ${HOME}"
    assert settings.csv_path.parts[-2:] == ("data", "log.csv")


def test_missing_env_file_says_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="Copy .env.example to .env"):
        load_settings(tmp_path / ".env")
