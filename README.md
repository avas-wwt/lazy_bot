# lazy_bot

Log your day once, and let lazy_bot type the same hours into every timesheet you have to fill.

Each day, you add a line to `timesheet.csv`:

```csv
date,hours,description
2026-10-12,8,Sprint planning; API integration
2026-10-13,7.5,Code review and bug fixes
```

Every Friday at 16:00 a scheduled task runs these steps:
1. Opens each platform in its own Edge profile, signs in with what's in your `.env` (you do MFA), and fills and saves the week.
2. Prints a summary and asks **`Go ahead? [y/N]`** before anything is submitted.
3. Submits everywhere.
4. Copies the week to `archive/<monday>_to_<sunday>.csv` and removes those rows from `timesheet.csv`.

| Platform | How | Status |
|---|---|---|
| Clockify | REST API (personal API key) | written, to be tried with a real key |
| Timesheet Portal (Cloud Decisions UK) | Edge | not written yet |
| WWT Kronos (UKG) | Edge, WWT Okta sign-in | not written yet |
| GlobalSource (Deel) | Edge; Deel has no drafts, so entries are only created after your "y" | not written yet |

## Setup (Windows)

1. Install Python 3.13 for your user, then create the environment from this folder:
   ```powershell
   winget install --id Python.Python.3.13 --exact --source winget --scope user
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
   Playwright uses the Edge you already have, so there's no `playwright install`.
2. Copy `.env.example` to `.env` and fill in your values. `.env` is git-ignored; never commit it.
3. Create `timesheet.csv` with the line `date,hours,description`.
4. Sign in to each platform once. The sessions are kept in `%LOCALAPPDATA%\lazy_bot\profiles\<platform>`:
   ```powershell
   .venv\Scripts\python -m lazy_bot login
   ```

## Commands

```powershell
.venv\Scripts\python -m lazy_bot check                 # validate the CSV, list due weeks (no browser)
.venv\Scripts\python -m lazy_bot run                   # process every due week
.venv\Scripts\python -m lazy_bot run --mode save       # fill and save only
.venv\Scripts\python -m lazy_bot run --platform kronos # just one platform
.venv\Scripts\python -m lazy_bot run --week 2026-10-05 # a specific week, due or not
.venv\Scripts\python -m lazy_bot login [--platform X]  # sign in once per platform
.venv\Scripts\python -m lazy_bot inspect --platform X  # save page snapshots (for writing adapters)
```

## The CSV

- **Dates**: `2026-10-12`, or whatever `DATE_FORMATS` allows. The default also accepts Excel's US `10/12/2026`.
- **Hours**: `8`, `7.5`, `7,5` or `7:30`. Several rows on one date are added up, and their descriptions joined with `; `.
- **Excel**: `,` or `;` separated files both work, as do UTF-8 (with or without BOM) and Windows-1252. When lazy_bot clears the file, it keeps the same format.
- **Validation**: lazy_bot checks the whole file before opening any browser and lists every bad line. A missing Mon–Fri day is only a warning.

## When is a week processed?

A week (Monday to Sunday) is **due** from its `RUN_DAY` at `RUN_TIME` (Friday 16:00 by default). If the laptop was off on Friday, the next run picks last week up and leaves this week's rows alone. Several due weeks are processed oldest first.

`state.json` remembers what each platform already has, so re-running is always safe:
- Finished platforms are skipped.
- A platform that failed is retried.
- A draft saved from data you later edited is refilled.
- Use `--force` to redo everything.

The week is archived and cleared only when **every** platform in `PLATFORMS` has finished it.

## Safety

- `.env`, `timesheet.csv`, `archive/`, `logs/` and `state.json` are git-ignored, because they hold your passwords and your work log.
- Passwords are optional. Leave one empty and type it yourself in the Edge window when a session expires. MFA/2FA is always yours to do.
- When a step fails, lazy_bot saves a screenshot and the page's HTML to `logs/<run>/` and carries on with the other platforms.
- **Deel (GlobalSource)**:
  - It runs bot detection, and its terms may restrict automated access.
  - lazy_bot creates entries there only after your "y", and makes no attempt to disguise the automation.
  - If Deel ever shows a verification challenge or a warning, stop automating it and enter the week by hand.

## Development

```powershell
.venv\Scripts\python -m pytest
```

The tests never touch your real `.env`, CSV or Edge profiles. `tests/test_e2e_fake.py` drives the real Edge (headless) through a whole run against `tests/fixtures/fake_timesheet.html`.

### Adding or fixing a platform

1. Copy `lazy_bot/platforms/fake.py`. It's the smallest complete adapter: `login()`, `fill_week()` (set values, never append, then save) and `submit()`.
2. Register it in `lazy_bot/platforms/__init__.py`, and add its `<NAME>_*` settings to `.env.example`.
3. To see a page's structure, run `python -m lazy_bot inspect --platform <name>`. It saves the HTML, the accessibility tree and a screenshot of each page you open into `logs/inspect/`.
