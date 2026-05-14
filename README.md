# Company/Organization — Payroll Attendance Reconciliation

Reconciles Hikvision device attendance against Zoho People leave records to compute overtime payroll, then appends results to a master Zoho Sheet and emails the recipient.

## What it does

For each working day (Sun–Thu in Saudi) of the target month, per employee:

| Hikvision shows                    | Leave system says    | Result                                                         |
| ---------------------------------- | -------------------- | -------------------------------------------------------------- |
| Nothing                            | On approved leave    | Absent (excused)                                               |
| Nothing                            | No leave             | Absent (unexcused)                                             |
| Check-in only                      | No leave             | Present, no overtime (Hikvision auto-closes at +15h)           |
| Check-in + check-out, < 9h         | No leave             | Present, no overtime                                           |
| Check-in + check-out, ≥ 9h         | No leave             | Present + overtime (capped at 6h beyond the 9h standard day)   |
| Check-in (any kind)                | On approved leave    | **Flag for manual review**                                     |

Saeed's preferences applied:
- Earliest check-in + latest check-out (middle taps ignored)
- 9h standard workday, 6h overtime cap (=15h Hikvision auto-checkout)
- Outputs appended to a master Zoho Sheet + email to `lalturki20@gmail.com`

## File map

```
config.py               — All credentials, IDs, and tunable constants
zoho_auth.py            — OAuth refresh-token manager (shared by all Zoho clients)
hikvision_client.py     — ISAPI client + event-to-shift collapser
zoho_people_client.py   — Approved-leave fetcher
zoho_sheet_client.py    — Reads contracts, appends report rows
salary_calendar.py      — Scrapes saudicalendars.com, decides when to fire
attendance_engine.py    — The 3-constraint reconciliation logic
mailer.py               — Zoho Mail API sender + HTML body builder
main.py                 — Orchestrates everything
```

## Setup

```bash
cd /Users/anwabiosciences/Desktop/zoho_contacts
source venv/bin/activate
cd /path/to/this/folder
pip install -r requirements.txt
```

## Configuration checklist

Edit `config.py` (or set environment variables) with:

### Hikvision (blocked on installer credentials)
- `HIKVISION_HOST` — currently `192.168.8.11`
- `HIKVISION_USER`, `HIKVISION_PASS` — admin credentials from the installer
- **Verify `HIKVISION_MAJOR_EVENT_TYPE` / `HIKVISION_MINOR_EVENT_TYPE`** against your device's actual event log. Different firmwares use different codes. The values in config (5/75) are the most common but not universal.

### Zoho OAuth
Generate at https://api-console.zoho.com (Self Client flow):
- `ZOHO_CLIENT_ID`
- `ZOHO_CLIENT_SECRET`
- `ZOHO_REFRESH_TOKEN` — minted with scopes:
  - `ZohoPeople.leave.READ`
  - `ZohoSheet.dataAPI.READ`
  - `ZohoSheet.dataAPI.UPDATE`
  - `ZohoMail.messages.CREATE`
- `ZOHO_REGION` — usually `com`; use `sa` if your account is on the Saudi data center

### Zoho resource IDs
From the URL of each open sheet (`https://sheet.zoho.com/sheet/open/<ID>`):
- `ZOHO_SHEET_CONTRACTS_ID` — the contracts sheet (employee id, name, OT rate)
- `ZOHO_SHEET_MASTER_REPORT_ID` — the master report sheet to append to

### Column names
Verify these match your contracts-sheet header row exactly:
- `CONTRACT_COL_EMPLOYEE_ID`
- `CONTRACT_COL_NAME`
- `CONTRACT_COL_ARRANGEMENT`
- `CONTRACT_COL_OT_RATE`

### Leave types
Run this once in Postman with your access token:
```
GET https://people.zoho.com/people/api/leavetracker/leavetypes
```
Then update `LEAVE_TYPES_SICK`, `LEAVE_TYPES_ANNUAL`, etc. to match exact spelling.

### Email
- `EMAIL_SENDER` — your Zoho Mail address (the from-address)
- `EMAIL_SENDER_ACCOUNT_ID` — get via `GET https://mail.zoho.com/api/accounts`

## Running

### First run (Jan + Feb 2026 backfill for comparison)
`config.BACKFILL_MODE = True` is the default. Just run:

```bash
python main.py
```

It will process both months unconditionally and email the recipient.

### Recurring mode (after backfill verified)
Set `config.BACKFILL_MODE = False`, then schedule:

```cron
0 6 * * * cd /path/to/payroll && /Users/anwabiosciences/Desktop/zoho_contacts/venv/bin/python main.py
```

(6:00 UTC = 9:00 AM Riyadh). The script self-gates:
1. Returns immediately if today's day-of-month is outside 21–29
2. Otherwise scrapes the salary calendar to find this month's drop date
3. Only fires the report when exactly 2 days remain

## Known caveats (read before trusting numbers)

1. **Hikvision is blocked** on the admin credentials your installer holds. The ISAPI client is ready to go but won't return data until those credentials are recovered.

2. **Event codes (5/75) are an assumption.** Cross-check against `/ISAPI/AccessControl/AcsEvent` raw output before trusting payroll numbers. If the codes are wrong, you'll see zero events.

3. **Salary calendar scraper is fragile.** Saudicalendars.com returned 403 to default `requests` UAs, so we spoof a Chrome User-Agent. If they tighten anti-scraping, the script logs a loud error and refuses to run silently. Don't ignore that log line — payroll missing the WPS deadline triggers MHRSD penalties.

4. **Overnight shifts not handled.** If someone checks in at 22:00 and out at 02:00 the next day, only the check-in's date counts. Adjust `hikvision_client.get_shifts` if Anwa runs night shifts.

5. **Zoho Sheet column names are case-sensitive.** If `Employee ID` in your sheet is actually `Employee Id` or `EmployeeID`, the contracts read will skip rows silently. Verify before the first run.

6. **Saudi workweek hardcoded as Sun–Thu.** Edit `attendance_engine.is_workday` if your contractors work different days.

7. **Total payment is *not* computed.** Per your instruction we only compute overtime hours and overtime pay. Base salary and final total payment remain a manual step or a future enhancement.
