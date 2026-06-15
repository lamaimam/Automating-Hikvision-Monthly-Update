"""
Zoho People client for fetching approved leave records.

Used to distinguish "absent" from "on approved leave" when an employee has no
Hikvision check-in for a workday.

API reference: https://www.zoho.com/people/api/leave-tracker.html
Endpoint: GET /people/api/forms/leave/getRecords
"""
import logging
from datetime import date, datetime
from typing import Dict, List, Set

import requests

import config
from zoho_auth import ZohoAuth

log = logging.getLogger(__name__)


class ZohoPeopleClient:
    """Fetches approved leave records from Zoho People."""

    def get_approved_leaves(self, start: date, end: date) -> Dict[str, Dict[date, str]]:
        """Return {employee_id: {date: leave_category}} for the window.

        leave_category is one of: "annual", "sick", "other".

        Includes only leaves with status 'Approved' that fall within or overlap
        the start..end window.
        """
        url = f"{config.ZOHO_PEOPLE_BASE}/forms/leave/getRecords"

        # Fetch ALL leave records with no server-side filter, then filter
        # client-side. The previous searchParams approach silently returned
        # zero records, likely due to filter syntax mismatch and/or because
        # it excluded leaves that started before the window even though
        # they overlapped it.
        params = {
            "sIndex": 1,
            "rec_limit": 200,
        }

        all_records: List[dict] = []
        s_index = 1
        page_count = 0
        while True:
            params["sIndex"] = s_index
            resp = requests.get(
                url,
                params=params,
                headers=ZohoAuth.auth_header(),
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()

            # Zoho People wraps records in 'response.result' which is a list
            # of single-key dicts: [{"<record_id>": [{...fields...}]}, ...]
            result = body.get("response", {}).get("result", [])
            page_count += 1
            if not result:
                if page_count == 1:
                    log.warning(
                        "Zoho People returned zero records on first page. "
                        "Full response: %s", body,
                    )
                break

            for entry in result:
                for _record_id, fields_list in entry.items():
                    if fields_list:
                        all_records.append(fields_list[0])

            if len(result) < params["rec_limit"]:
                break
            s_index += params["rec_limit"]

        log.info(
            "Zoho People: fetched %d total leave records across %d page(s) "
            "(before filtering by window/status)",
            len(all_records), page_count,
        )

        # Diagnostic: if we got records, log the field names of the first one
        # so we can spot field-name mismatches (Leavetype vs LeaveType etc.)
        if all_records:
            log.info(
                "Zoho People: first record field names: %s",
                sorted(all_records[0].keys()),
            )

        # Build {employee_id: {date: leave_category}}
        # leave_category is one of "annual", "sick", or "other"
        leave_map: Dict[str, Dict[date, str]] = {}
        skipped_unapproved = 0
        skipped_wrong_type = 0
        skipped_no_emp_id = 0
        skipped_bad_dates = 0
        skipped_out_of_window = 0
        matched_count = 0
        seen_leave_types: Dict[str, int] = {}

        for rec in all_records:
            status = (rec.get("ApprovalStatus") or "").strip().lower()
            if status != "approved":
                skipped_unapproved += 1
                continue

            leave_type = (rec.get("Leavetype") or "").strip()
            seen_leave_types[leave_type] = seen_leave_types.get(leave_type, 0) + 1
            if leave_type not in config.ALL_APPROVED_LEAVE_TYPES:
                skipped_wrong_type += 1
                continue

            # Categorize the leave for downstream grid coding
            if leave_type in config.LEAVE_TYPES_ANNUAL:
                category = "annual"
            elif leave_type in config.LEAVE_TYPES_SICK:
                category = "sick"
            else:
                category = "other"

            emp_id_raw = str(rec.get("EmployeeID") or rec.get("Employee_ID") or "").strip()
            if not emp_id_raw:
                skipped_no_emp_id += 1
                continue

            # Zoho stores Employee_ID as a compound string like
            # "Mohammed Al Khalaf 218" or just "218". Extract the trailing
            # numeric ID (the part after the last whitespace).
            import re
            id_match = re.search(r"(\d+)\s*$", emp_id_raw)
            if id_match:
                emp_id = id_match.group(1)
            else:
                emp_id = emp_id_raw  # fall back if no trailing number

            # Zoho People returns dates as "DD-Mon-YYYY" e.g. "19-May-2026",
            # not ISO. Parse with that format. Also tolerate ISO in case the
            # API behaviour changes.
            def _parse_zoho_date(s: str) -> date:
                s = s.strip()
                # Try DD-Mon-YYYY first (Zoho's actual format)
                try:
                    return datetime.strptime(s[:11], "%d-%b-%Y").date()
                except ValueError:
                    pass
                # Fall back to ISO
                return date.fromisoformat(s[:10])

            try:
                from_date = _parse_zoho_date(rec["From"])
                to_date = _parse_zoho_date(rec["To"])
            except (KeyError, ValueError) as e:
                log.warning("Could not parse leave dates for record: %s (%s)", rec, e)
                skipped_bad_dates += 1
                continue

            # Skip leaves that don't overlap our window at all.
            if to_date < start or from_date > end:
                skipped_out_of_window += 1
                continue

            # Expand the overlapping portion into individual dates.
            cur = max(from_date, start)
            stop = min(to_date, end)
            while cur <= stop:
                leave_map.setdefault(emp_id, {})[cur] = category
                cur = date.fromordinal(cur.toordinal() + 1)
            matched_count += 1

        log.info(
            "Zoho People filter results: matched=%d, "
            "skipped_unapproved=%d, skipped_wrong_type=%d, "
            "skipped_no_emp_id=%d, skipped_bad_dates=%d, "
            "skipped_out_of_window=%d",
            matched_count, skipped_unapproved, skipped_wrong_type,
            skipped_no_emp_id, skipped_bad_dates, skipped_out_of_window,
        )
        if seen_leave_types:
            log.info(
                "Zoho People: leave types seen in approved records: %s",
                dict(sorted(seen_leave_types.items(), key=lambda x: -x[1])),
            )
            log.info(
                "Zoho People: leave types in config allowlist: %s",
                config.ALL_APPROVED_LEAVE_TYPES,
            )

        return leave_map