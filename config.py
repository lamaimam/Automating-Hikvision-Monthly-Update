"""
Configuration for Anwa BioSciences payroll-attendance reconciliation.

All secrets should be loaded from environment variables in production.
For this first run, fill in the placeholders directly to test.
"""
import os
from datetime import date

# ============================================================================
# RUN MODE
# ============================================================================
# For the first run, set BACKFILL_MODE = True to process Jan + Feb 2026
# explicitly. When you switch to recurring daily mode, set it to False and the
# script will use the salary-calendar logic to decide when to fire.
BACKFILL_MODE = True
BACKFILL_MONTHS = [(2026, 1), (2026, 2)]

# ============================================================================
# WORK RULES
# ============================================================================SS
STANDARD_WORKDAY_HOURS = 9
HIKVISION_AUTO_CHECKOUT_HOURS = 15
OVERTIME_CAP_HOURS = HIKVISION_AUTO_CHECKOUT_HOURS - STANDARD_WORKDAY_HOURS  # 6

# ============================================================================
# HIKVISION (ISAPI direct from device)
# ============================================================================
HIKVISION_HOST = os.getenv("HIKVISION_HOST", "REPLACE_ME") #e.g. 000.00.00.00
HIKVISION_PORT = int(os.getenv("HIKVISION_PORT", "80"))
HIKVISION_USER = os.getenv("HIKVISION_USER", "admin")
HIKVISION_PASS = os.getenv("HIKVISION_PASS", "REPLACE_ME")  # blocked on installer
HIKVISION_SCHEME = "http"  # use "https" if your device is configured for it

# Event codes for access-granted (check-in/out). The exact major/minor codes
# depend on device firmware; 5/75 is the common "Card swipe authenticated"
# pair on access control terminals. Verify in your device's event log before
# trusting this in production.
HIKVISION_MAJOR_EVENT_TYPE = 5
HIKVISION_MINOR_EVENT_TYPE = 75

# ============================================================================
# ZOHO OAUTH
# ============================================================================
# Generate these via Zoho's self-client flow:
# https://api-console.zoho.com  -> Self Client -> Create
ZOHO_REGION = "com"  # use "sa" if your account is on the Saudi data center
ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "REPLACE_ME")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "REPLACE_ME")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "REPLACE_ME")

ZOHO_ACCOUNTS_BASE = f"https://accounts.zoho.{ZOHO_REGION}"
ZOHO_PEOPLE_BASE = f"https://people.zoho.{ZOHO_REGION}/people/api"
ZOHO_SHEET_BASE = f"https://sheet.zoho.{ZOHO_REGION}/api/v2"
ZOHO_MAIL_BASE = f"https://mail.zoho.{ZOHO_REGION}/api"

# ============================================================================
# ZOHO PEOPLE - LEAVE
# ============================================================================
# Map your Zoho People leave-type display names to internal categories.
# Run this in Postman first to confirm exact spelling:
#   GET {ZOHO_PEOPLE_BASE}/leavetracker/leavetypes
LEAVE_TYPES_SICK = ["Sick Leave", "Medical Leave"]
LEAVE_TYPES_ANNUAL = ["Annual Leave", "Vacation"]
LEAVE_TYPES_OTHER_APPROVED = ["Hajj Leave", "Marriage Leave", "Bereavement Leave"]
ALL_APPROVED_LEAVE_TYPES = (
    LEAVE_TYPES_SICK + LEAVE_TYPES_ANNUAL + LEAVE_TYPES_OTHER_APPROVED
)

# ============================================================================
# ZOHO SHEET - INPUT (contracts) AND OUTPUT (master report)
# ============================================================================
# Resource IDs come from the Zoho Sheet URL:
#   https://sheet.zoho.com/sheet/open/<RESOURCE_ID>
ZOHO_SHEET_CONTRACTS_ID = os.getenv("ZOHO_SHEET_CONTRACTS_ID", "REPLACE_ME")
ZOHO_SHEET_CONTRACTS_WORKSHEET = "Sheet1"

ZOHO_SHEET_MASTER_REPORT_ID = os.getenv("ZOHO_SHEET_MASTER_REPORT_ID", "REPLACE_ME")
ZOHO_SHEET_MASTER_REPORT_WORKSHEET = "Sheet1"

# Column names expected in the contracts sheet (case-sensitive, must match
# the sheet header row exactly).
CONTRACT_COL_EMPLOYEE_ID = "Employee ID"
CONTRACT_COL_NAME = "Name"
CONTRACT_COL_ARRANGEMENT = "Arrangement Type"
CONTRACT_COL_OT_RATE = "Overtime Pay Rate"
# Hours and total payment are intentionally NOT read - we recalculate them.

# ============================================================================
# SALARY CALENDAR
# ============================================================================
SALARY_CALENDAR_URL = "https://saudicalendars.com/salaries-dates/"
SALARY_CHECK_WINDOW = (21, 29)  # inclusive day-of-month range
SALARY_FIRE_DAYS_BEFORE = 2

# ============================================================================
# EMAIL
# ============================================================================
EMAIL_RECIPIENT = "REPLACE_ME@gmail.com"
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "REPLACE_ME@company.org")
EMAIL_SENDER_ACCOUNT_ID = os.getenv("EMAIL_SENDER_ACCOUNT_ID", "REPLACE_ME")

# ============================================================================
# LOGGING
# ============================================================================
LOG_DIR = os.getenv("LOG_DIR", "PATH TO WHERE YOU WANT IT TO BE")
LOG_LEVEL = "INFO"
