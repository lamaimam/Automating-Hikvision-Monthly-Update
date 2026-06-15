"""
Attendance reconciliation engine.

Applies the constraints Saeed specified:

  R1 - No check-in AND no check-out on a workday:
       - If approved leave: mark by category (V annual / S sick / other excused)
       - Otherwise: assume PRESENT (not absent)

  R2 - Check-in but no check-out:
       - Assume standard 9h workday. Zero overtime.

  R3 - Check-in AND check-out:
       3a - duration < 9h:       present, zero OT
       3b - 9h <= duration < 17h: present, OT = duration - 9h (regular = 9h)
       3c - duration >= 17h:     FLAG for review, zero OT
                                 (forgot to check out, or stray late event)

  R4 - Check-out WITHOUT check-in:
       - FLAG for review, zero OT

  R5 - Approved leave AND check-in on same day:
       - FLAG for review

Note: There is no hard 6-hour OT cap any more. The 17h window naturally
caps real OT at just under 8 hours (17 - 9). Anything beyond 17h gets
zero OT and a review flag, not a capped value.
"""
import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Set

import config
from hikvision_client import AttendanceShift

log = logging.getLogger(__name__)

# Hard upper bound on a single shift's duration. Anything at or beyond this
# is treated as a sensor anomaly, not real OT.
SHIFT_MAX_HOURS = 17.0


@dataclass
class DayResult:
    """One employee's reconciled record for one day."""
    work_date: date
    status: str  # "present_no_ot", "present_with_ot", "absent_excused",
                 # "assumed_present", "needs_review",
                 # "annual_leave", "sick_leave"
    overtime_hours: float = 0.0
    notes: str = ""

    @property
    def grid_code(self) -> str:
        """Cell content for the day-grid.

        Three values:
          - "P" - present (any kind of presence)
          - "V" - on approved annual leave
          - "S" - on approved sick leave
        """
        if self.status == "annual_leave":
            return "V"
        if self.status == "sick_leave":
            return "S"
        return "P"


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
    """Every day is a workday per Saeed's policy. No weekends."""
    return True


def month_workdays(year: int, month: int) -> List[date]:
    """All days in the given month - every day is a workday."""
    _, last_day = calendar.monthrange(year, month)
    return [date(year, month, d) for d in range(1, last_day + 1)]


def reconcile_day(
    workday: date,
    shift: Optional[AttendanceShift],
    leave_category: Optional[str] = None,
) -> DayResult:
    """Apply the constraints to a single employee-day.

    leave_category: None / "annual" / "sick" / "other"
    """
    has_checkin = bool(shift and shift.check_in)
    has_checkout = bool(shift and shift.check_out)
    on_approved_leave = leave_category is not None

    # R5: approved leave but also checked in -> review
    if on_approved_leave and has_checkin:
        return DayResult(
            work_date=workday,
            status="needs_review",
            notes=(
                f"On approved leave ({leave_category}) but checked in at "
                f"{shift.check_in.strftime('%H:%M')}"
            ),
        )

    # R1: no Hikvision events at all
    if not has_checkin and not has_checkout:
        if leave_category == "annual":
            return DayResult(work_date=workday, status="annual_leave",
                             notes="On annual leave")
        if leave_category == "sick":
            return DayResult(work_date=workday, status="sick_leave",
                             notes="On sick leave")
        if leave_category == "other":
            return DayResult(work_date=workday, status="absent_excused",
                             notes="On approved leave (other)")
        # No leave request = assume present (not absent), zero OT.
        return DayResult(
            work_date=workday,
            status="assumed_present",
            notes="No Hikvision event, no leave - assumed present",
        )

    # R4: check-out without check-in -> flag for review
    if not has_checkin and has_checkout:
        return DayResult(
            work_date=workday,
            status="needs_review",
            notes=(
                f"Check-out at {shift.check_out.strftime('%H:%M')} "
                f"without any check-in"
            ),
        )

    # R2: check-in but no check-out -> assume standard 9h day, zero OT
    if has_checkin and not has_checkout:
        return DayResult(
            work_date=workday,
            status="present_no_ot",
            overtime_hours=0.0,
            notes=(
                f"Checked in at {shift.check_in.strftime('%H:%M')}, "
                f"no check-out recorded; assumed standard "
                f"{config.STANDARD_WORKDAY_HOURS}h day"
            ),
        )

    # R3: both check-in AND check-out
    duration = shift.duration_hours
    if duration is None:
        # Shouldn't happen given both timestamps exist, but be defensive.
        return DayResult(
            work_date=workday,
            status="needs_review",
            notes="Both check-in and check-out present but duration unknown",
        )

    # R3c: duration at or beyond 17h -> sensor anomaly, flag, zero OT
    if duration >= SHIFT_MAX_HOURS:
        return DayResult(
            work_date=workday,
            status="needs_review",
            overtime_hours=0.0,
            notes=(
                f"Duration {duration:.2f}h is at or beyond the {SHIFT_MAX_HOURS}h "
                f"limit (check-in {shift.check_in.strftime('%H:%M')} -> "
                f"check-out {shift.check_out.strftime('%H:%M')}); flagged"
            ),
        )

    # R3a: under 9h -> present, zero OT
    if duration < config.STANDARD_WORKDAY_HOURS:
        return DayResult(
            work_date=workday,
            status="present_no_ot",
            overtime_hours=0.0,
            notes=(
                f"Worked {duration:.2f}h "
                f"(under {config.STANDARD_WORKDAY_HOURS}h, no OT)"
            ),
        )

    # R3b: 9h <= duration < 17h -> real OT
    overtime = duration - config.STANDARD_WORKDAY_HOURS
    return DayResult(
        work_date=workday,
        status="present_with_ot",
        overtime_hours=overtime,
        notes=(
            f"Worked {duration:.2f}h => {overtime:.2f}h OT "
            f"({shift.check_in.strftime('%H:%M')} -> "
            f"{shift.check_out.strftime('%H:%M')})"
        ),
    )


def compute_monthly_payroll(
    employee_id: str,
    name: str,
    overtime_rate: float,
    year: int,
    month: int,
    shifts_by_date: Dict[date, AttendanceShift],
    leave_categories: Dict[date, str],
) -> MonthlyPayroll:
    """Reconcile a single employee's attendance for a single month.

    leave_categories maps date -> "annual" / "sick" / "other".
    """
    payroll = MonthlyPayroll(
        employee_id=employee_id,
        name=name,
        year=year,
        month=month,
        overtime_rate=overtime_rate,
    )

    for workday in month_workdays(year, month):
        shift = shifts_by_date.get(workday)
        leave_cat = leave_categories.get(workday)  # None if not on leave
        day_result = reconcile_day(workday, shift, leave_cat)
        payroll.day_results.append(day_result)

        if day_result.status == "present_no_ot":
            payroll.days_present += 1
        elif day_result.status == "present_with_ot":
            payroll.days_present += 1
            payroll.total_overtime_hours += day_result.overtime_hours
        elif day_result.status == "assumed_present":
            payroll.days_present += 1
        elif day_result.status in ("annual_leave", "sick_leave", "absent_excused"):
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