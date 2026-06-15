"""
Main entry point for the Anwa BioSciences payroll-attendance reconciliation.

Two ways the pipeline is invoked:

  1. Programmatic (the normal path):
       main.run_period(start, end) is called by scheduler.py when its
       state machine determines today is the chosen send day. The full
       pipeline runs for the rolling period [start, end].

  2. Backfill mode (config.BACKFILL_MODE = True):
       Processes config.BACKFILL_MONTHS unconditionally as full calendar
       months. Used for one-off manual reprocessing.

The old "salary-calendar recurring mode" that used payroll_period.py and
saudicalendars.com is gone - replaced by scheduler.py's email-prompt
state machine. To trigger a manual run, edit files/scheduler_state.json
and run: python scheduler.py --action run_pipeline
"""
import calendar
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import config
from attendance_engine import MonthlyPayroll, compute_monthly_payroll
from hikvision_client import HikvisionClient
from mailer import build_summary_html, send_report_email
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


# ----------------------------------------------------------------------
# Per-calendar-month processing (used by BACKFILL mode - unchanged)
# ----------------------------------------------------------------------
def process_month(
    year: int,
    month: int,
    sheet_client: ZohoSheetClient,
    hikvision_client: HikvisionClient,
    people_client: ZohoPeopleClient,
    log: logging.Logger,
) -> List[MonthlyPayroll]:
    """Process a single calendar month end-to-end."""
    log.info("=" * 60)
    log.info("Processing %d-%02d (full calendar month)", year, month)
    log.info("=" * 60)

    contracts = sheet_client.read_contracts(year, month)
    log.info("Loaded %d employee contracts", len(contracts))

    _, last_day = calendar.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    return _process_range(
        contracts, start, end, year, month,
        hikvision_client, people_client, log,
    )


# ----------------------------------------------------------------------
# Rolling-period processing (used by RECURRING mode and run_period)
# ----------------------------------------------------------------------
def _process_range(
    contracts,
    start: date,
    end: date,
    label_year: int,
    label_month: int,
    hikvision_client: HikvisionClient,
    people_client: ZohoPeopleClient,
    log: logging.Logger,
) -> List[MonthlyPayroll]:
    """Shared engine: fetch attendance + leave for [start, end] and compute payroll.

    label_year / label_month are used by the payroll engine for output labelling
    (which month does this payroll belong to). For rolling periods, use the
    month of the salary date.
    """
    shifts_by_employee = hikvision_client.get_shifts(start, end)
    leaves_by_employee = people_client.get_approved_leaves(start, end)

    results: List[MonthlyPayroll] = []
    for c in contracts:
        payroll = compute_monthly_payroll(
            employee_id=c.employee_id,
            name=c.name,
            overtime_rate=c.overtime_rate,
            year=label_year,
            month=label_month,
            shifts_by_date=shifts_by_employee.get(c.employee_id, {}),
            leave_categories=leaves_by_employee.get(c.employee_id, {}),
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


def process_period(
    start: date,
    end: date,
    sheet_client: ZohoSheetClient,
    hikvision_client: HikvisionClient,
    people_client: ZohoPeopleClient,
    log: logging.Logger,
) -> List[MonthlyPayroll]:
    """Process a rolling payroll period [start, end] end-to-end.

    Uses end.month / end.year for labelling (since salary release is in `end`).
    Reads contracts from the month tab matching `end`.
    """
    log.info("=" * 60)
    log.info(
        "Processing rolling period %s -> %s (label: %d-%02d)",
        start.isoformat(), end.isoformat(), end.year, end.month,
    )
    log.info("=" * 60)

    contracts = sheet_client.read_contracts(end.year, end.month)
    log.info("Loaded %d employee contracts", len(contracts))

    return _process_range(
        contracts, start, end, end.year, end.month,
        hikvision_client, people_client, log,
    )


# ----------------------------------------------------------------------
# Output building (unchanged)
# ----------------------------------------------------------------------
def payroll_to_employee_updates(payrolls: List[MonthlyPayroll]) -> List[dict]:
    """Build one update dict per employee for the summary columns only."""
    rows = []
    for p in payrolls:
        rows.append({
            config.CONTRACT_COL_EMPLOYEE_ID: p.employee_id,
            config.MASTER_COL_TOTAL_HOURS: round(p.total_overtime_hours, 2),
            config.MASTER_COL_TOTAL_PAYMENT: round(p.total_overtime_pay, 2),
        })
    return rows


def payroll_to_day_grid_cells(
    payrolls: List[MonthlyPayroll],
) -> Dict[int, Dict[str, str]]:
    """Build the per-day cell map for the day grid."""
    cells: Dict[int, Dict[str, str]] = {}
    for p in payrolls:
        for dr in p.day_results:
            day_num = dr.work_date.day
            cells.setdefault(day_num, {})[p.employee_id] = dr.grid_code
    return cells


def collect_review_items(payrolls: List[MonthlyPayroll]) -> List[str]:
    items = []
    for p in payrolls:
        for note in p.review_notes:
            items.append(f"{p.name} ({p.employee_id}): {note}")
    return items


# ----------------------------------------------------------------------
# Pipeline that writes results to sheets + sends email
# ----------------------------------------------------------------------
def _write_and_email(
    payrolls: List[MonthlyPayroll],
    label_year: int,
    label_month: int,
    sheet_client: ZohoSheetClient,
    log: logging.Logger,
    email_subject: str,
) -> None:
    """Write results to sheets and send the report email."""
    # 1. Summary update: Total Hrs + Total payment in master sheet
    sheet_client.update_employee_rows(
        label_year, label_month, payroll_to_employee_updates(payrolls)
    )

    # 2. Day grid: write ONLY to the master report (contracts is read-only input)
    day_cells = payroll_to_day_grid_cells(payrolls)
    ws_name = date(label_year, label_month, 1).strftime(
        config.ZOHO_SHEET_MASTER_REPORT_WORKSHEET_FORMAT
    )
    days_in_month = calendar.monthrange(label_year, label_month)[1]
    sheet_client.write_day_grid(
        config.ZOHO_SHEET_MASTER_REPORT_ID, ws_name, days_in_month, day_cells
    )

    # 3. Email
    sheet_url = (
        f"https://sheet.zoho.{config.ZOHO_REGION}/sheet/open/"
        f"{config.ZOHO_SHEET_MASTER_REPORT_ID}"
    )
    send_report_email(
        subject=email_subject,
        body_html=build_summary_html(
            year=label_year,
            month=label_month,
            employee_count=len(payrolls),
            review_items=collect_review_items(payrolls),
            sheet_url=sheet_url,
        ),
    )
    log.info("Sheets updated and email sent.")


# ----------------------------------------------------------------------
# Public API for scheduler.py
# ----------------------------------------------------------------------
def run_period(start: date, end: date) -> int:
    """Run the full payroll pipeline for an explicit period.

    Called by scheduler.py when launchd determines today is fire date.
    Returns 0 on success, non-zero on failure.
    """
    log = setup_logging()
    sheet_client = ZohoSheetClient()
    hikvision_client = HikvisionClient()
    people_client = ZohoPeopleClient()

    payrolls = process_period(
        start, end, sheet_client, hikvision_client, people_client, log,
    )
    _write_and_email(
        payrolls,
        label_year=end.year,
        label_month=end.month,
        sheet_client=sheet_client,
        log=log,
        email_subject=(
            f"[Anwa Payroll] {end.year}-{end.month:02d} - "
            f"period {start.isoformat()} to {end.isoformat()}"
        ),
    )
    return 0


# ----------------------------------------------------------------------
# CLI entry point (only for backfill or manual recurring trigger)
# ----------------------------------------------------------------------
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
            _write_and_email(
                payrolls,
                label_year=year,
                label_month=month,
                sheet_client=sheet_client,
                log=log,
                email_subject=(
                    f"[Anwa Payroll] Backfill {year}-{month:02d}"
                ),
            )
            all_payrolls.extend(payrolls)
            all_review_items.extend(collect_review_items(payrolls))
            months_processed.append((year, month))

        log.info("Backfill complete: %d months processed.", len(months_processed))
        return 0

    # ------------------------------------------------------------------
    # Mode 2: recurring trigger
    # ------------------------------------------------------------------
    # The old recurring mode used payroll_period.py (salary-calendar scraper)
    # to decide when to fire. That trigger logic has been replaced by
    # scheduler.py's email-prompt flow. To run the pipeline outside of
    # backfill, set scheduler_state.json and call:
    #     python scheduler.py --action run_pipeline
    log.error(
        "BACKFILL_MODE is False. The old salary-calendar trigger has been "
        "replaced by scheduler.py. To run the pipeline manually, edit "
        "files/scheduler_state.json and run: "
        "python scheduler.py --action run_pipeline"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
