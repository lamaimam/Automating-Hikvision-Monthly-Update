"""
Main entry point for the Anwa BioSciences payroll-attendance reconciliation.

Two modes:

  1. Backfill mode (config.BACKFILL_MODE = True):
       Processes config.BACKFILL_MONTHS unconditionally. Use this for the
       first run to compare against Jan + Feb 2026 manual calculations.

  2. Recurring mode (config.BACKFILL_MODE = False):
       Should be run daily via cron. Internally:
         - Returns immediately if today is outside the polling window (21-29)
         - Otherwise scrapes the salary date; only fires when 2 days remain
         - Processes the previous full month's attendance

Run:
    python main.py

Cron suggestion (daily at 9am Riyadh):
    0 6 * * * cd /path/to/payroll && /path/to/venv/bin/python main.py
    (6 UTC = 9am AST)
"""
import calendar
import logging
import os
import sys
from datetime import date, datetime
from typing import List, Tuple

import config
from attendance_engine import MonthlyPayroll, compute_monthly_payroll
from hikvision_client import HikvisionClient
from mailer import build_summary_html, send_report_email
from salary_calendar import should_fire_today
from zoho_people_client import ZohoPeopleClient
from zoho_sheet_client import ZohoSheetClient


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_path = os.path.join(
        config.LOG_DIR,
        f"payroll-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log",
    )
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("main")


def process_month(
    year: int,
    month: int,
    sheet_client: ZohoSheetClient,
    hikvision_client: HikvisionClient,
    people_client: ZohoPeopleClient,
    log: logging.Logger,
) -> List[MonthlyPayroll]:
    """Process a single month end-to-end."""
    log.info("=" * 60)
    log.info("Processing %d-%02d", year, month)
    log.info("=" * 60)

    contracts = sheet_client.read_contracts()
    log.info("Loaded %d employee contracts", len(contracts))

    _, last_day = calendar.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    shifts_by_employee = hikvision_client.get_shifts(start, end)
    leaves_by_employee = people_client.get_approved_leaves(start, end)

    results: List[MonthlyPayroll] = []
    for c in contracts:
        payroll = compute_monthly_payroll(
            employee_id=c.employee_id,
            name=c.name,
            overtime_rate=c.overtime_rate,
            year=year,
            month=month,
            shifts_by_date=shifts_by_employee.get(c.employee_id, {}),
            leave_dates=leaves_by_employee.get(c.employee_id, set()),
        )
        results.append(payroll)
        log.info(
            "%s (%s): present=%d, excused=%d, unexcused=%d, "
            "review=%d, OT_hrs=%.2f, OT_pay=%.2f",
            c.name, c.employee_id,
            payroll.days_present,
            payroll.days_absent_excused,
            payroll.days_absent_unexcused,
            payroll.days_needing_review,
            payroll.total_overtime_hours,
            payroll.total_overtime_pay,
        )

    return results


def payroll_to_sheet_rows(payrolls: List[MonthlyPayroll]) -> List[dict]:
    """Flatten payroll objects into one row-per-employee dicts for the sheet."""
    rows = []
    for p in payrolls:
        rows.append({
            "Period": f"{p.year}-{p.month:02d}",
            "Employee ID": p.employee_id,
            "Name": p.name,
            "Days Present": str(p.days_present),
            "Days Excused Absent": str(p.days_absent_excused),
            "Days Unexcused Absent": str(p.days_absent_unexcused),
            "Days Needing Review": str(p.days_needing_review),
            "Total Overtime Hours": f"{p.total_overtime_hours:.2f}",
            "Overtime Rate (SAR/hr)": f"{p.overtime_rate:.2f}",
            "Total Overtime Pay (SAR)": f"{p.total_overtime_pay:.2f}",
            "Review Notes": " | ".join(p.review_notes) if p.review_notes else "",
            "Generated At": datetime.now().isoformat(timespec="seconds"),
        })
    return rows


def collect_review_items(payrolls: List[MonthlyPayroll]) -> List[str]:
    items = []
    for p in payrolls:
        for note in p.review_notes:
            items.append(f"{p.name} ({p.employee_id}): {note}")
    return items


def main() -> int:
    log = setup_logging()

    sheet_client = ZohoSheetClient()
    hikvision_client = HikvisionClient()
    people_client = ZohoPeopleClient()

    # ------------------------------------------------------------------
    # Mode 1: explicit backfill (Jan + Feb 2026 for comparison)
    # ------------------------------------------------------------------
    if config.BACKFILL_MODE:
        log.info("Running in BACKFILL mode for months: %s", config.BACKFILL_MONTHS)
        all_payrolls: List[MonthlyPayroll] = []
        all_review_items: List[str] = []
        months_processed: List[Tuple[int, int]] = []

        for (year, month) in config.BACKFILL_MONTHS:
            payrolls = process_month(
                year, month, sheet_client, hikvision_client, people_client, log
            )
            sheet_client.append_report_rows(payroll_to_sheet_rows(payrolls))
            all_payrolls.extend(payrolls)
            all_review_items.extend(collect_review_items(payrolls))
            months_processed.append((year, month))

        months_label = ", ".join(f"{y}-{m:02d}" for y, m in months_processed)
        sheet_url = (
            f"https://sheet.zoho.{config.ZOHO_REGION}/sheet/open/"
            f"{config.ZOHO_SHEET_MASTER_REPORT_ID}"
        )
        send_report_email(
            subject=f"[Anwa Payroll] Backfill report: {months_label}",
            body_html=build_summary_html(
                year=months_processed[-1][0],
                month=months_processed[-1][1],
                employee_count=len(all_payrolls),
                review_items=all_review_items,
                sheet_url=sheet_url,
            ),
        )
        log.info("Backfill complete.")
        return 0

    # ------------------------------------------------------------------
    # Mode 2: recurring daily poll
    # ------------------------------------------------------------------
    today = date.today()
    fire, salary_date = should_fire_today(today)
    if not fire:
        if salary_date:
            log.info(
                "Not firing today. Next salary: %s (%d days away)",
                salary_date, (salary_date - today).days,
            )
        else:
            log.info("Outside polling window or salary date unknown; exiting.")
        return 0

    # Fire: process the *previous* full month
    if today.month == 1:
        target_year, target_month = today.year - 1, 12
    else:
        target_year, target_month = today.year, today.month - 1

    log.info(
        "Firing payroll report 2 days before salary (%s). Target month: %d-%02d",
        salary_date, target_year, target_month,
    )

    payrolls = process_month(
        target_year, target_month,
        sheet_client, hikvision_client, people_client, log,
    )
    sheet_client.append_report_rows(payroll_to_sheet_rows(payrolls))

    sheet_url = (
        f"https://sheet.zoho.{config.ZOHO_REGION}/sheet/open/"
        f"{config.ZOHO_SHEET_MASTER_REPORT_ID}"
    )
    send_report_email(
        subject=f"[Anwa Payroll] {target_year}-{target_month:02d} - "
                f"salary drops {salary_date.isoformat()}",
        body_html=build_summary_html(
            year=target_year,
            month=target_month,
            employee_count=len(payrolls),
            review_items=collect_review_items(payrolls),
            sheet_url=sheet_url,
        ),
    )
    log.info("Recurring run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
