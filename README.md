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
| Clockify | REST API (personal API key) | working (real entry created, re-run changed nothing) |
| Timesheet Portal (Cloud Decisions UK) | Edge; one timesheet per month: weekly "Save draft", submitted in the week holding the month's last day | working (real draft saved and read back); monthly Submit not yet exercised |
| WWT Kronos (UKG) | Edge, WWT Okta sign-in, follows the WWT "My Time User Guide" | working (real save with comment, re-run changed nothing); Approve not yet exercised |
| GlobalSource (Deel) | Edge at app.deel.com (the branded hiring.globalsource.cloud rejects time tracking with "Origin not allowed"); no drafts, so each day is submitted only after your "y"; signs in with your .env details, 2FA stays yours | written and rehearsed up to Deel's review step; first real submission happens on a supervised run |

## Setup (Windows)

1. Clone the repo, then run this in its folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```
   It does the following:
   - Installs Python 3.13 for your user if needed (winget, no admin).
   - Creates `.venv` and installs the packages. Playwright uses the Edge you already have, so there's nothing else to download.
   - Creates `.env` and `timesheet.csv` if they're missing.
   - Registers the **"lazy_bot timesheets"** scheduled task. It runs every `RUN_DAY` at `RUN_TIME`, and 3 minutes after you sign in to Windows to catch up on a missed run.

   You can run it again at any time. `-SkipTask` leaves the task alone, and `-RemoveTask` deletes it.
2. Fill in `.env`. It's git-ignored; never commit it.
3. Sign in to each platform once. The sessions are kept in `%LOCALAPPDATA%\lazy_bot\profiles\<platform>`:
   ```powershell
   .venv\Scripts\python -m lazy_bot login
   ```

## Commands

```powershell
.venv\Scripts\python -m lazy_bot check                 # validate the CSV, list due weeks (no browser)
.venv\Scripts\python -m lazy_bot run                   # process every due week
.venv\Scripts\python -m lazy_bot run --mode save       # fill and save only
.venv\Scripts\python -m lazy_bot run --rehearse --week 2026-10-12  # type it all in, save nothing (try-out)
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

## WWT Kronos: what lazy_bot does

It follows WWT's *My Time User Guide*:
1. **Show activities**, and open the week. Only this week and last week are supported; older weeks are entered by hand.
2. **Days off**: each weekday up to today with no hours gets a `0` on the **Home** line first (holidays, sick days).
3. **Activity line**: if missing, it's added with **Add activity**. lazy_bot searches `KRONOS_ACTIVITY_QUERY` (your code + `*`), opens it, and picks the child containing `KRONOS_ACTIVITY` ("Project Cost Collection", never "Non-Billable").
4. **Hours** go on that project line. Kronos fills the Home line itself.
5. **Description**: each day's description becomes an `Activity Comment` on that day's cell.
6. **Save**. After your "y", **Approve** (`KRONOS_APPROVE=true`).

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
