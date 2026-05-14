"""
Hikvision ISAPI client for access-control attendance events.

Pulls AcsEvent records from the device, groups them by employee + date,
and produces clean shift records: (earliest check-in, latest check-out).

Reference: Hikvision ISAPI v2.X - Access Control Event Search
Endpoint: POST /ISAPI/AccessControl/AcsEvent?format=json
"""
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

import requests
from requests.auth import HTTPDigestAuth

import config

log = logging.getLogger(__name__)


@dataclass
class AttendanceShift:
    """One employee's attendance picture for one day."""
    employee_id: str
    work_date: date
    check_in: Optional[datetime]
    check_out: Optional[datetime]
    raw_event_count: int  # for debugging: how many taps did we collapse?

    @property
    def status(self) -> str:
        """Categorize the shift for downstream payroll logic."""
        if self.check_in and self.check_out:
            return "complete"
        if self.check_in and not self.check_out:
            return "no_checkout"
        if not self.check_in and not self.check_out:
            return "no_record"
        # Check-out without check-in: edge case, treat like no_record + flag
        return "no_checkin_only"

    @property
    def duration_hours(self) -> Optional[float]:
        if not (self.check_in and self.check_out):
            return None
        return (self.check_out - self.check_in).total_seconds() / 3600.0


class HikvisionClient:
    def __init__(self):
        self.base_url = (
            f"{config.HIKVISION_SCHEME}://{config.HIKVISION_HOST}:{config.HIKVISION_PORT}"
        )
        self.auth = HTTPDigestAuth(config.HIKVISION_USER, config.HIKVISION_PASS)

    def fetch_raw_events(self, start: date, end: date) -> List[dict]:
        """Fetch all access-granted events between start and end (inclusive).

        ISAPI returns max 30 events per page; we paginate via searchResultPosition.
        """
        all_events: List[dict] = []
        position = 0
        page_size = 30

        # ISO 8601 with timezone offset is required by ISAPI
        start_iso = f"{start.isoformat()}T00:00:00+03:00"  # Saudi is UTC+3
        end_iso = f"{end.isoformat()}T23:59:59+03:00"

        while True:
            payload = {
                "AcsEventCond": {
                    "searchID": "anwa-payroll-fetch",
                    "searchResultPosition": position,
                    "maxResults": page_size,
                    "major": config.HIKVISION_MAJOR_EVENT_TYPE,
                    "minor": config.HIKVISION_MINOR_EVENT_TYPE,
                    "startTime": start_iso,
                    "endTime": end_iso,
                }
            }

            log.debug("Hikvision AcsEvent fetch position=%d", position)
            resp = requests.post(
                f"{self.base_url}/ISAPI/AccessControl/AcsEvent?format=json",
                json=payload,
                auth=self.auth,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("AcsEvent", {})

            matches = data.get("InfoList", [])
            all_events.extend(matches)

            total = data.get("totalMatches", 0)
            num_in_page = data.get("numOfMatches", 0)
            log.info(
                "Hikvision: fetched %d/%d events (page returned %d)",
                len(all_events), total, num_in_page,
            )

            if num_in_page < page_size or len(all_events) >= total:
                break
            position += num_in_page

        return all_events

    def get_shifts(self, start: date, end: date) -> Dict[str, Dict[date, AttendanceShift]]:
        """Group events into per-employee, per-day shifts.

        Per Saeed: take earliest check-in and latest check-out, ignore middle taps.
        Returns: {employee_id: {date: AttendanceShift}}
        """
        events = self.fetch_raw_events(start, end)

        # Bucket events by (employee_id, date)
        buckets: Dict[tuple, List[datetime]] = defaultdict(list)
        for ev in events:
            emp_id = ev.get("employeeNoString") or str(ev.get("employeeNo", ""))
            ts_str = ev.get("time")  # e.g. "2026-01-15T08:23:11+03:00"
            if not emp_id or not ts_str:
                log.warning("Hikvision event missing employeeNo or time: %s", ev)
                continue

            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                log.warning("Could not parse timestamp: %s", ts_str)
                continue

            # Use the local date - shifts that cross midnight belong to the
            # check-in date. (No overnight shifts expected at Anwa, but if a
            # shift checks out after midnight we'd want to attach it to the
            # check-in's date, not the check-out's date. Out of scope for v1.)
            buckets[(emp_id, ts.date())].append(ts)

        # Collapse each bucket into earliest/latest
        result: Dict[str, Dict[date, AttendanceShift]] = defaultdict(dict)
        for (emp_id, work_date), timestamps in buckets.items():
            timestamps.sort()
            check_in = timestamps[0] if timestamps else None
            check_out = timestamps[-1] if len(timestamps) > 1 else None

            result[emp_id][work_date] = AttendanceShift(
                employee_id=emp_id,
                work_date=work_date,
                check_in=check_in,
                check_out=check_out,
                raw_event_count=len(timestamps),
            )

        log.info(
            "Hikvision: parsed %d events into %d employee-days",
            len(events),
            sum(len(days) for days in result.values()),
        )
        return result
