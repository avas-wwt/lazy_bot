"""Command line: python -m lazy_bot check | login | run | screenshot | inspect."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import truststore

from .config import SUBMIT_MODES, ConfigError, Settings, load_settings
from .platforms import Platform, PlatformError, adapter_class, make_platform
from .state import State
from .timesheet import TimesheetError, TimesheetFile, Week, read_timesheet, week_start

log = logging.getLogger("lazy_bot")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # an odd character in a description must not crash a run
    # Trust the same certificates as Windows: company networks that inspect HTTPS (e.g. Netskope) re-sign
    # every site with their own root certificate, which Windows knows and Python's bundled list doesn't.
    truststore.inject_into_ssl()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
        return args.handler(settings, args)
    except (ConfigError, TimesheetError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except PlatformError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lazy_bot", description="Fill your weekly timesheets from timesheet.csv.")
    parser.add_argument("--env-file", type=Path, help="use this settings file instead of .env")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    check = commands.add_parser("check", help="check timesheet.csv and show which weeks are due (no browser)")
    check.set_defaults(handler=cmd_check)

    login = commands.add_parser("login", help="sign in once per platform (API platforms: check the key, read-only)")
    login.add_argument("--platform", action="append", help="only this platform (can be repeated)")
    login.set_defaults(handler=cmd_login)

    run = commands.add_parser("run", help="fill, confirm and submit every due week")
    run.add_argument("--week", type=date.fromisoformat, metavar="YYYY-MM-DD",
                     help="process the week that contains this date, due or not")
    run.add_argument("--platform", action="append", help="only this platform (can be repeated)")
    run.add_argument("--mode", choices=SUBMIT_MODES, help="override SUBMIT_MODE for this run")
    run.add_argument("--force", action="store_true", help="redo platforms that state.json says are finished")
    run.add_argument("--rehearse", action="store_true",
                     help="fill everything in on the sites but never save or submit (nothing is recorded)")
    run.add_argument("--pause", action="store_true", help="wait for Enter before closing (the scheduled task uses it)")
    run.set_defaults(handler=cmd_run)

    shot = commands.add_parser("screenshot", help="save a picture of a week's timecard, ready to share")
    shot.add_argument("--week", type=date.fromisoformat, metavar="YYYY-MM-DD",
                      help="the week that contains this date (default: this week)")
    shot.add_argument("--platform", action="append", help="only this platform (default: every one that supports it)")
    shot.set_defaults(handler=cmd_screenshot)

    inspect = commands.add_parser("inspect", help="save snapshots of the pages you open in a platform's profile")
    inspect.add_argument("--platform", required=True)
    inspect.add_argument("--every", type=float, default=0, metavar="SECONDS",
                         help="instead of waiting for Enter, save whenever a page changes (checked every N seconds)")
    inspect.set_defaults(handler=cmd_inspect)
    return parser


# Commands


def cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    timesheet = read_timesheet(settings.csv_path, settings.date_formats)
    state = State.load(settings.state_path)
    weeks = timesheet.weeks()
    print(f"{timesheet.path}: {len(timesheet.rows)} row(s) in {len(weeks)} week(s) "
          f"(delimiter '{timesheet.delimiter}', {timesheet.encoding})")
    for warning in timesheet.warnings:
        print(f"  ! {warning}")
    now = datetime.now()
    for week in weeks:
        due = week.due_at(settings.run_day, settings.run_time)
        when = "due now" if now >= due else f"due {due:%a %Y-%m-%d %H:%M}"
        print(f"  {week.label}  {week.hours_text():>6} h  {when}")
        for day in week.missing_weekdays():
            print(f"      ! {day:%a %Y-%m-%d} has no entry")
        done = state.data["weeks"].get(week.key, {}).get("platforms", {})
        for name, record in done.items():
            if record.get("status"):
                stale = " (CSV changed since)" if record.get("hash") != week.data_hash() else ""
                print(f"      {name}: {record['status']} {record.get('at', '')}{stale}")
    if not weeks:
        print("  (no rows yet)")
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    from .browser import BrowserPool
    from .runner import Runner

    timesheet = read_timesheet(settings.csv_path, settings.date_formats)
    weeks = select_weeks(timesheet, settings, args.week)
    if not weeks:
        print("Nothing due yet.")
        return 0
    run_dir = settings.logs_dir / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    setup_logging(run_dir)
    for warning in timesheet.warnings:
        log.warning("! %s", warning)
    state = State.load(settings.state_path)
    platforms = make_platforms(settings, state, args.platform)
    with BrowserPool(settings) as pool:
        runner = Runner(
            settings, state, timesheet, platforms,
            mode=args.mode or settings.submit_mode,
            run_dir=run_dir,
            only=set(args.platform) if args.platform else None,
            force=args.force,
            rehearse=args.rehearse,
            open_session=pool.open,
            confirm=ask_yes_no,
            retry_locked=ask_retry,
            hold=read_line,
        )
        ok = runner.run(weeks)
    log.info("Log: %s", run_dir / "run.log")
    if args.pause:
        read_line("Press Enter to close this window...")
    return 0 if ok else 1


def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    from playwright.sync_api import Error as PlaywrightError

    from .browser import BrowserPool

    names = args.platform or list(settings.platforms)
    if not names:
        raise ConfigError("PLATFORMS is empty in .env")
    state = State.load(settings.state_path)
    failed = False
    with BrowserPool(settings) as pool:
        for name in names:
            platform = make_platform(settings, name, state.cache(name))
            if not platform.uses_browser:
                platform.cache.clear()  # look everything up again
                try:
                    platform.login()
                except (ConfigError, PlatformError) as exc:
                    print(f"[{platform.name}] {exc}")
                    failed = True
                    continue
                finally:
                    state.save()
                print(f"[{platform.name}] {platform.describe() or 'signed in'}")
                continue
            print(f"[{platform.name}] Sign in to {platform.title} in the Edge window that opens "
                  "(tick 'stay signed in' if offered), then close the window.")
            session = pool.open(platform)
            try:
                session.page.goto(platform.url, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                print(f"[{platform.name}] Couldn't open {platform.url}: {str(exc).splitlines()[0]}")
            session.wait_until_closed()
            session.close()
            print(f"[{platform.name}] Done; the sign-in is kept in {settings.profile_dir(platform.name)}")
    return 1 if failed else 0


def cmd_screenshot(settings: Settings, args: argparse.Namespace) -> int:
    from .browser import BrowserPool

    names = [name.lower() for name in args.platform or settings.platforms]
    if not args.platform:
        names = [name for name in names if adapter_class(name).supports_screenshot]
    if not names:
        raise ConfigError("no platform in PLATFORMS takes screenshots (try --platform kronos)")
    setup_logging()
    week = Week(week_start(args.week or date.today()), [], [])
    failed = False
    with BrowserPool(settings) as pool:
        for name in names:
            platform = make_platform(settings, name, {})
            if not platform.supports_screenshot:
                print(f"[{platform.name}] {platform.title} doesn't take screenshots.")
                failed = True
                continue
            try:
                platform.session = pool.open(platform)
                platform.login()
                print(f"[{platform.name}] {platform.screenshot(week)}")
            except (ConfigError, PlatformError) as exc:
                print(f"[{platform.name}] {exc}")
                failed = True
            finally:
                if platform.session is not None:
                    platform.session.close()
    return 1 if failed else 0


def cmd_inspect(settings: Settings, args: argparse.Namespace) -> int:
    from playwright.sync_api import Error as PlaywrightError

    from .browser import BrowserPool
    from .snapshots import Snapshotter

    platform = make_platform(settings, args.platform, {})
    if not platform.uses_browser:
        print(f"{platform.title} uses an API, so there are no pages to inspect.")
        return 0
    folder = settings.logs_dir / "inspect" / platform.name / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    with BrowserPool(settings) as pool:
        session = pool.open(platform)
        try:
            session.page.goto(platform.url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            print(f"Couldn't open {platform.url}: {str(exc).splitlines()[0]}")
        snapper = Snapshotter(session, folder)
        if args.every > 0:
            print(f"Saving a snapshot whenever a page changes into {folder}. Close the Edge window to stop.")
            while session.is_open():
                if not snapper.full and snapper.capture(only_changes=True):
                    print(f"  {snapper.count} snapshot(s) so far" + (" (the maximum)" if snapper.full else ""))
                session.pump(int(args.every * 1000))
        else:
            print(f"Press Enter to save a snapshot of every open tab into {folder}; type q and Enter to stop.")
            while session.is_open() and not snapper.full:
                answer = read_line("> ", session.pump)
                if answer is None or answer.strip().lower() == "q":
                    break
                snapper.capture()
                print(f"  {snapper.count} snapshot(s) so far")
        session.close()
    print(f"{snapper.count} snapshot(s) saved in {folder}")
    return 0


# Helpers


def select_weeks(timesheet: TimesheetFile, settings: Settings, day: date | None) -> list[Week]:
    weeks = timesheet.weeks()
    if day is not None:
        chosen = [week for week in weeks if week.contains(day)]
        if not chosen:
            raise TimesheetError(timesheet.path, [f"there are no rows in the week of {day}"])
        return chosen
    now = datetime.now()
    return [week for week in weeks if now >= week.due_at(settings.run_day, settings.run_time)]


def make_platforms(settings: Settings, state: State, extra: list[str] | None) -> list[Platform]:
    names = list(dict.fromkeys([*settings.platforms, *(name.lower() for name in extra or [])]))
    if not names:
        raise ConfigError("PLATFORMS is empty in .env (e.g. PLATFORMS=clockify,timesheetportal,kronos,globalsource)")
    return [make_platform(settings, name, state.cache(name)) for name in names]


def setup_logging(run_dir: Path | None = None) -> None:
    logger = logging.getLogger("lazy_bot")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        file = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
        file.setLevel(logging.DEBUG)
        file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logger.addHandler(file)


def read_line(prompt: str, pump: Callable[[], None] | None = None) -> str | None:
    """input() that keeps open browsers serviced while it waits. None when there's no console to type in."""
    if sys.stdin is None or not sys.stdin.isatty():
        return None
    answer: list[str] = []

    def read() -> None:
        try:
            answer.append(input(prompt))
        except (EOFError, OSError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    while reader.is_alive():
        if pump is not None:
            pump()
        else:
            reader.join(0.2)
    return answer[0] if answer else None


def ask_yes_no(question: str, pump: Callable[[], None]) -> bool:
    answer = read_line(question, pump)
    log.debug("%s-> %r", question, answer)
    if answer is None:
        log.info("(no console to type an answer in)")
        return False
    return answer.strip().lower() in ("y", "yes")


def ask_retry(message: str) -> bool:
    answer = read_line(message)
    return answer is not None and answer.strip().lower() not in ("s", "skip")
