"""
Hikvision ISAPI client for access-control attendance events.

Pulls AcsEvent records from the device, groups them by employee + date,
and produces clean shift records: (earliest check-in, latest check-out).

Reference: Hikvision ISAPI v2.X - Access Control Event Search
Endpoint: POST /ISAPI/AccessControl/AcsEvent?format=json

Constraints learned from this specific device (DS-K1T341AMF, firmware V3.2.30):
  - ISAPI rejects multi-day queries with "Invalid Operation".
    => Chunk requests one day at a time.
  - Reusing searchID across calls can trigger errors on some firmware.
    => Use a unique searchID per (day, position) pair.
  - The device emits 5-10x noise events per real punch. Only events with
    minor == 38 carry employee identity and attendanceStatus.
    => Filter on minor == 38 at parse time.
  - Querying for a minor code that doesn't exist on this device (e.g. 75)
    returns 401 Unauthorized instead of an empty result set.
    => config.HIKVISION_MINOR_EVENT_TYPE must be 38 for this device.
  - The digest auth nonce issued by the device appears to expire after
    ~20 successful calls when reused via requests.HTTPDigestAuth.
    Subsequent calls return 401 without recovery.
    => Use a fresh requests.Session() for every day so the digest
       handshake is always negotiated from scratch.
  - When a day fails, the script previously raced through the next ~10
    days in milliseconds, hammering the device.
    => Sleep runs in a finally block so it executes on success AND failure.
       After a failure, an additional cooldown gives the device time to recover.
"""
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests
from requests.auth import HTTPDigestAuth

import config

log = logging.getLogger(__name__)

# Hikvision event sub-type that carries real employee attendance data.
ATTENDANCE_MINOR_CODE = 38

# Per-call result cap on the device (firmware default).
PAGE_SIZE = 30

# Polite delay between calls (runs whether the call succeeded or failed).
INTER_CALL_DELAY_SEC = 1.0

# Additional cooldown after a failed day, to give the device time to recover
# from whatever state caused the 401.
POST_FAILURE_COOLDOWN_SEC = 5.0


@dataclass
class AttendanceShift:
    """One employee's attendance picture for one day."""
    employee_id: str
    work_date: date
    check_in: Optional[datetime]
    check_out: Optional[datetime]
    raw_event_count: int

    @property
    def status(self) -> str:
        if self.check_in and self.check_out:
            return "complete"
        if self.check_in and not self.check_out:
            return "no_checkout"
        if not self.check_in and not self.check_out:
            return "no_record"
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
        # Track days that failed so we can report them at the end.
        self.failed_days: List[date] = []

    def _make_session(self) -> requests.Session:
        """Build a fresh Session with its own digest auth state.

        Each Session gets its own HTTPDigestAuth instance, which means the
        digest handshake (and its nonce) is negotiated from scratch when
        the session makes its first request. This avoids the 401-after-N-calls
        pattern caused by nonce expiration in long-lived auth state.
        """
        session = requests.Session()
        session.auth = HTTPDigestAuth(config.HIKVISION_USER, config.HIKVISION_PASS)
        return session

    def _post_acsevent(self, session: requests.Session, payload: dict) -> dict:
        """POST one AcsEvent query using the provided session. No retry."""
        url = f"{self.base_url}/ISAPI/AccessControl/AcsEvent?format=json"
        resp = session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json().get("AcsEvent", {})

    def _fetch_one_day(self, day: date) -> List[dict]:
        """Fetch all AcsEvent records for a single day, paginating internally.

        Uses a fresh Session so each day starts with a clean digest handshake.
        """
        events: List[dict] = []
        position = 0
        page_count = 0

        start_iso = f"{day.isoformat()}T00:00:00+03:00"
        end_iso = f"{day.isoformat()}T23:59:59+03:00"

        # Fresh session per day - context manager closes the connection cleanly.
        with self._make_session() as session:
            while True:
                payload = {
                    "AcsEventCond": {
                        "searchID": f"anwa-{day.isoformat()}-{position}",
                        "searchResultPosition": position,
                        "maxResults": PAGE_SIZE,
                        "major": config.HIKVISION_MAJOR_EVENT_TYPE,
                        "minor": config.HIKVISION_MINOR_EVENT_TYPE,
                        "startTime": start_iso,
                        "endTime": end_iso,
                    }
                }

                data = self._post_acsevent(session, payload)
                page_count += 1

                matches = data.get("InfoList", []) or []
                events.extend(matches)

                total = data.get("totalMatches", 0)
                num_in_page = data.get("numOfMatches", 0)

                if num_in_page == 0 or num_in_page < PAGE_SIZE or len(events) >= total:
                    break
                position += num_in_page

                # Throttle between pages within the same day.
                time.sleep(INTER_CALL_DELAY_SEC)

        log.info(
            "Hikvision %s: %d raw events fetched (across %d page(s))",
            day.isoformat(), len(events), page_count,
        )
        return events

    def fetch_raw_events(self, start: date, end: date) -> List[dict]:
        """Fetch events between start and end (inclusive), one day per ISAPI call.

        Critical: the inter-day delay runs in a finally block, so it executes
        whether the day succeeded or failed. This prevents the script from
        racing through failures in milliseconds and hammering the device.
        """
        all_events: List[dict] = []
        current = start
        days_processed = 0

        while current <= end:
            day_failed = False
            try:
                day_events = self._fetch_one_day(current)
                all_events.extend(day_events)
            except requests.HTTPError as e:
                log.error(
                    "Hikvision %s: failed with %s - skipping this day. "
                    "Check device manually for this date.",
                    current.isoformat(), e,
                )
                self.failed_days.append(current)
                day_failed = True
            except requests.RequestException as e:
                log.error(
                    "Hikvision %s: network error %s - skipping this day.",
                    current.isoformat(), e,
                )
                self.failed_days.append(current)
                day_failed = True
            finally:
                # ALWAYS sleep, whether success or failure. This is the critical
                # fix that prevents the millisecond-cascade-of-failures pattern.
                if day_failed:
                    time.sleep(POST_FAILURE_COOLDOWN_SEC)
                else:
                    time.sleep(INTER_CALL_DELAY_SEC)

            current += timedelta(days=1)
            days_processed += 1

        log.info(
            "Hikvision: fetched %d total raw events across %d day(s) "
            "(%s -> %s); %d day(s) failed",
            len(all_events), days_processed,
            start.isoformat(), end.isoformat(),
            len(self.failed_days),
        )
        if self.failed_days:
            log.warning(
                "Hikvision: failed days require manual review: %s",
                ", ".join(d.isoformat() for d in self.failed_days),
            )
        return all_events

    def get_shifts(self, start: date, end: date) -> Dict[str, Dict[date, AttendanceShift]]:
        """Group events into per-employee, per-day shifts.

        Logic:
          1. Filter to minor == 38 (real attendance events with employeeNo).
          2. For each (employee, date) bucket, split by attendanceStatus.
          3. Earliest checkIn = day's check-in.
          4. Latest checkOut = day's check-out.
          5. If attendanceStatus is missing, fall back to time ordering.
        """
        events = self.fetch_raw_events(start, end)

        buckets: Dict[tuple, List[tuple]] = defaultdict(list)
        skipped_non_attendance = 0
        skipped_no_id = 0

        for ev in events:
            if ev.get("minor") != ATTENDANCE_MINOR_CODE:
                skipped_non_attendance += 1
                continue

            emp_id = ev.get("employeeNoString") or str(ev.get("employeeNo", "") or "")
            ts_str = ev.get("time")
            if not emp_id or not ts_str:
                skipped_no_id += 1
                continue

            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                log.warning("Could not parse timestamp: %s", ts_str)
                continue

            status = ev.get("attendanceStatus")
            buckets[(emp_id, ts.date())].append((ts, status))

        log.info(
            "Hikvision parse: %d events kept, %d non-attendance skipped, "
            "%d missing-id skipped",
            sum(len(v) for v in buckets.values()),
            skipped_non_attendance, skipped_no_id,
        )

        result: Dict[str, Dict[date, AttendanceShift]] = defaultdict(dict)

        for (emp_id, work_date), entries in buckets.items():
            entries.sort(key=lambda x: x[0])

            check_ins = [ts for ts, status in entries if status == "checkIn"]
            check_outs = [ts for ts, status in entries if status == "checkOut"]

            if check_ins or check_outs:
                check_in = check_ins[0] if check_ins else None
                check_out = check_outs[-1] if check_outs else None
            else:
                timestamps = [ts for ts, _ in entries]
                check_in = timestamps[0] if timestamps else None
                check_out = timestamps[-1] if len(timestamps) > 1 else None

            result[emp_id][work_date] = AttendanceShift(
                employee_id=emp_id,
                work_date=work_date,
                check_in=check_in,
                check_out=check_out,
                raw_event_count=len(entries),
            )

        log.info(
            "Hikvision: parsed events into %d employee-days across %d employees",
            sum(len(days) for days in result.values()),
            len(result),
        )
        return result