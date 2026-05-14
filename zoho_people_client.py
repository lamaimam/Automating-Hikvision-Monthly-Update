"""
Zoho People client for fetching approved leave records.

Used to distinguish "absent" from "on approved leave" when an employee has no
Hikvision check-in for a workday.

API reference: https://www.zoho.com/people/api/leave-tracker.html
Endpoint: GET /people/api/forms/leave/getRecords
"""
import logging
from datetime import date
from typing import Dict, List, Set

import requests

import config
from zoho_auth import ZohoAuth

log = logging.getLogger(__name__)


class ZohoPeopleClient:
    """Fetches approved leave records from Zoho People."""

    def get_approved_leaves(self, start: date, end: date) -> Dict[str, Set[date]]:
        """Return {employee_id: {set of dates on approved leave}} for the window.

        Includes only leaves with status 'Approved' that fall within or overlap
        the start..end window. Returns dates an employee is excused from work.
        """
        url = f"{config.ZOHO_PEOPLE_BASE}/forms/leave/getRecords"
        params = {
            "sIndex": 1,
            "rec_limit": 200,
            "searchParams": (
                '{"searchField":"From","searchOperator":"Between",'
                f'"searchText":"{start.isoformat()};{end.isoformat()}"' + "}"
            ),
        }

        all_records: List[dict] = []
        s_index = 1
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
            if not result:
                break

            for entry in result:
                for _record_id, fields_list in entry.items():
                    if fields_list:
                        all_records.append(fields_list[0])

            if len(result) < params["rec_limit"]:
                break
            s_index += params["rec_limit"]

        log.info("Zoho People: fetched %d leave records", len(all_records))

        # Build {employee_id: {date, date, ...}}
        leave_map: Dict[str, Set[date]] = {}
        for rec in all_records:
            status = (rec.get("ApprovalStatus") or "").strip().lower()
            if status != "approved":
                continue

            leave_type = (rec.get("Leavetype") or "").strip()
            if leave_type not in config.ALL_APPROVED_LEAVE_TYPES:
                log.debug("Skipping leave type not in allowlist: %s", leave_type)
                continue

            emp_id = str(rec.get("EmployeeID") or rec.get("Employee_ID") or "").strip()
            if not emp_id:
                continue

            try:
                from_date = date.fromisoformat(rec["From"][:10])
                to_date = date.fromisoformat(rec["To"][:10])
            except (KeyError, ValueError) as e:
                log.warning("Could not parse leave dates for record: %s (%s)", rec, e)
                continue

            # Expand the range into individual dates within our window
            cur = max(from_date, start)
            stop = min(to_date, end)
            while cur <= stop:
                leave_map.setdefault(emp_id, set()).add(cur)
                cur = date.fromordinal(cur.toordinal() + 1)

        return leave_map
