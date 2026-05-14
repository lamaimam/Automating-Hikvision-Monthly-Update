"""
Attendance reconciliation engine.

Applies the three constraints Saeed described:

  1. No check-in AND no check-out on a workday:
       - If approved leave (sick/annual/etc.) -> absent (excused)
       - Otherwise -> absent (unexcused, flagged)

  2. Check-in but no check-out:
       - Hikvision auto-closes at +15h. If we never see a check-out, treat
         the day as a standard 9-hour workday with NO overtime.

  3. Check-in AND check-out:
       - Duration >= 9h:  9h regular + overtime up to OVERTIME_CAP_HOURS (6h)
       - Duration <  9h:  regular hours only, no overtime

  Special: approved leave + actual check-in on same day -> flag for manual review.
"""
import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Set

import config
from hikvision_client import AttendanceShift

log = logging.getLogger(__name__)


@dataclass
class DayResult:
    """One employee's reconciled record for one day."""
    work_date: date
    status: str  # "present_no_ot", "present_with_ot", "absent_excused",
                 # "absent_unexcused", "needs_review"
    overtime_hours: float = 0.0
    notes: str = ""


@dataclass
class MonthlyPayroll:
    """One employee's reconciled payroll for one month."""
    employee_id: str
    name: str
    year: int
    month: int
    overtime_rate: float
    total_overtime_hours: float = 0.0
    total_overtime_pay: float = 0.0
    days_present: int = 0
    days_absent_excused: int = 0
    days_absent_unexcused: int = 0
    days_needing_review: int = 0
    review_notes: List[str] = field(default_factory=list)
    day_results: List[DayResult] = field(default_factory=list)


def is_workday(d: date) -> bool:
    """Saudi work week is Sunday-Thursday. Friday and Saturday are weekend.

    In Python's datetime, Monday=0...Sunday=6.
    Friday=4, Saturday=5 -> off.
    """
    return d.weekday() not in (4, 5)


def month_workdays(year: int, month: int) -> List[date]:
    """All Sun-Thu working days in the given month."""
    _, last_day = calendar.monthrange(year, month)
    return [
        date(year, month, d)
        for d in range(1, last_day + 1)
        if is_workday(date(year, month, d))
    ]


def reconcile_day(
    workday: date,
    shift: Optional[AttendanceShift],
    on_approved_leave: bool,
) -> DayResult:
    """Apply the three constraints to a single employee-day."""

    has_checkin = bool(shift and shift.check_in)
    has_checkout = bool(shift and shift.check_out)

    # Edge case Saeed asked us to flag: approved leave but also checked in.
    if on_approved_leave and has_checkin:
        return DayResult(
            work_date=workday,
            status="needs_review",
            notes=f"On approved leave but checked in at "
                  f"{shift.check_in.strftime('%H:%M')}",
        )

    # Constraint 1: no record at all
    if not has_checkin and not has_checkout:
        if on_approved_leave:
            return DayResult(
                work_date=workday,
                status="absent_excused",
                notes="On approved leave",
            )
        return DayResult(
            work_date=workday,
            status="absent_unexcused",
            notes="No check-in, no approved leave",
        )

    # Constraint 2: checked in but never checked out
    if has_checkin and not has_checkout:
        return DayResult(
            work_date=workday,
            status="present_no_ot",
            overtime_hours=0.0,
            notes="No check-out recorded; assumed standard 9h day",
        )

    # Constraint 3: both check-in and check-out
    duration = shift.duration_hours  # already a float
    if duration < config.STANDARD_WORKDAY_HOURS:
        return DayResult(
            work_date=workday,
            status="present_no_ot",
            overtime_hours=0.0,
            notes=f"Worked {duration:.2f}h (under {config.STANDARD_WORKDAY_HOURS}h)",
        )

    overtime = duration - config.STANDARD_WORKDAY_HOURS
    overtime_capped = min(overtime, config.OVERTIME_CAP_HOURS)

    note = f"Worked {duration:.2f}h => {overtime_capped:.2f}h OT"
    if overtime > config.OVERTIME_CAP_HOURS:
        note += f" (capped from {overtime:.2f}h)"

    return DayResult(
        work_date=workday,
        status="present_with_ot",
        overtime_hours=overtime_capped,
        notes=note,
    )


def compute_monthly_payroll(
    employee_id: str,
    name: str,
    overtime_rate: float,
    year: int,
    month: int,
    shifts_by_date: Dict[date, AttendanceShift],
    leave_dates: Set[date],
) -> MonthlyPayroll:
    """Reconcile a single employee's attendance for a single month."""

    payroll = MonthlyPayroll(
        employee_id=employee_id,
        name=name,
        year=year,
        month=month,
        overtime_rate=overtime_rate,
    )

    for workday in month_workdays(year, month):
        shift = shifts_by_date.get(workday)
        on_leave = workday in leave_dates
        day_result = reconcile_day(workday, shift, on_leave)
        payroll.day_results.append(day_result)

        if day_result.status == "present_no_ot":
            payroll.days_present += 1
        elif day_result.status == "present_with_ot":
            payroll.days_present += 1
            payroll.total_overtime_hours += day_result.overtime_hours
        elif day_result.status == "absent_excused":
            payroll.days_absent_excused += 1
        elif day_result.status == "absent_unexcused":
            payroll.days_absent_unexcused += 1
        elif day_result.status == "needs_review":
            payroll.days_needing_review += 1
            payroll.review_notes.append(
                f"{workday.isoformat()}: {day_result.notes}"
            )

    payroll.total_overtime_pay = round(
        payroll.total_overtime_hours * overtime_rate, 2
    )
    return payroll
