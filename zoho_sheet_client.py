"""
Zoho Sheet client - reads employee contracts and appends report rows.

API reference: https://www.zoho.com/sheet/help/api/v2/

Two operations needed:
  1. Read contracts from the input sheet (employee id, name, OT rate, etc.)
     - The contracts file has one tab per month: "Jan 2026", "Feb 2026", etc.
  2. Append calculated rows to the master report sheet.
"""
import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import requests

import config
from zoho_auth import ZohoAuth

log = logging.getLogger(__name__)


def contract_worksheet_name(year: int, month: int) -> str:
    """Build the per-month worksheet tab name, e.g. 'Jan 2026'."""
    return date(year, month, 1).strftime(config.ZOHO_SHEET_CONTRACTS_WORKSHEET_FORMAT)


@dataclass
class EmployeeContract:
    employee_id: str
    name: str
    arrangement: str  # "overtime" etc
    overtime_rate: float  # SAR per hour


class ZohoSheetClient:
    def _post(self, resource_id: str, method: str, payload: dict) -> dict:
        """Zoho Sheet API v2 uses form-encoded POSTs with 'method' and JSON params."""
        url = f"{config.ZOHO_SHEET_BASE}/{resource_id}"
        data = {
            "method": method,
            **{k: json.dumps(v) if not isinstance(v, str) else v
               for k, v in payload.items()},
        }
        resp = requests.post(
            url,
            headers=ZohoAuth.auth_header(),
            data=data,
            timeout=30,
        )
        if resp.status_code >= 400:
            log.error("Zoho Sheet API error %d: %s", resp.status_code, resp.text)
            log.error("URL: %s", url)
            log.error("Method: %s", method)
            log.error("Payload keys: %s", list(data.keys()))
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Zoho Sheet {method} failed: {body}")
        return body

    def read_contracts(self, year: int, month: int) -> List[EmployeeContract]:
        """Read employee contract rows from the worksheet tab for the given month.

        e.g. read_contracts(2026, 1) reads the 'Jan 2026' tab.
        """
        worksheet_name = contract_worksheet_name(year, month)
        log.info("Reading contracts from worksheet tab '%s'", worksheet_name)

        body = self._post(
            config.ZOHO_SHEET_CONTRACTS_ID,
            "worksheet.records.fetch",
            {
                "worksheet_name": worksheet_name,
                "header_row": 1,
            },
        )
        records = body.get("records", [])
        log.info("Zoho Sheet: read %d contract rows", len(records))

        contracts = []
        for r in records:
            try:
                emp_id = str(r[config.CONTRACT_COL_EMPLOYEE_ID]).strip()
                if not emp_id:
                    continue
                contracts.append(EmployeeContract(
                    employee_id=emp_id,
                    name=str(r[config.CONTRACT_COL_NAME]).strip(),
                    arrangement=str(r[config.CONTRACT_COL_ARRANGEMENT]).strip(),
                    overtime_rate=float(r[config.CONTRACT_COL_OT_RATE]),
                ))
            except (KeyError, ValueError, TypeError) as e:
                log.warning("Skipping malformed contract row %s: %s", r, e)

        return contracts

    def update_employee_rows(
        self, year: int, month: int, updates: List[Dict[str, object]]
    ) -> int:
        """For each employee, update their existing row in the master sheet.

        updates is a list of dicts, one per employee, e.g.:
          [
            {"Employee ID": "218", "Total Hrs": 6.5, "Total payment": 514.09},
            ...
          ]

        Each item must contain "Employee ID" plus whichever columns to update.
        Writes to the monthly tab matching the period (e.g. "Jan 2026").

        Returns the number of rows successfully updated.
        """
        if not updates:
            log.info("No employee updates to apply")
            return 0

        worksheet_name = date(year, month, 1).strftime(
            config.ZOHO_SHEET_MASTER_REPORT_WORKSHEET_FORMAT
        )
        log.info(
            "Updating %d rows in master report tab '%s'", len(updates), worksheet_name
        )

        success_count = 0
        for upd in updates:
            emp_id = upd.get(config.CONTRACT_COL_EMPLOYEE_ID)
            if emp_id is None:
                log.warning("Skipping update without Employee ID: %s", upd)
                continue

            # Columns to update (everything except the matching key)
            data_map = {k: v for k, v in upd.items()
                        if k != config.CONTRACT_COL_EMPLOYEE_ID}
            if not data_map:
                continue

            # Zoho criteria syntax: "Column Name"="value"
            # Numeric IDs don't need quoting around the value, but text does;
            # we'll always quote the employee ID since it's stored as string.
            criteria = f'"{config.CONTRACT_COL_EMPLOYEE_ID}"="{emp_id}"'

            try:
                self._post(
                    config.ZOHO_SHEET_MASTER_REPORT_ID,
                    "worksheet.records.update",
                    {
                        "worksheet_name": worksheet_name,
                        "header_row": 1,
                        "criteria": criteria,
                        "data": data_map,
                    },
                )
                success_count += 1
            except Exception as e:
                log.error("Failed to update row for Employee ID %s: %s", emp_id, e)

        log.info(
            "Zoho Sheet: updated %d/%d rows in master report",
            success_count, len(updates),
        )
        return success_count

    def write_day_grid(
        self,
        resource_id: str,
        worksheet_name: str,
        days_in_month: int,
        cells_by_day: Dict[int, Dict[str, str]],
    ) -> int:
        """Write the per-day status grid into the worksheet.

        The day grid is a separate sub-table from the main contracts table,
        with its own header row. We tell Zoho where it lives using
        start_row/start_column/end_row/end_column so it doesn't try to
        merge with the contracts table on the left.

        Layout in the sheet:
          Row 1 (header): I=IDs, J=218, K=212, ... V=225
          Row 2:          (employee name letters - ignored by the API,
                           lives between header and data)
          Rows 3..33:     I=day_num, J..V=status code per employee

        cells_by_day is {day_number: {employee_id: status_code, ...}}

        Returns the number of day-rows successfully updated.
        """
        if not cells_by_day:
            log.info("No day-grid cells to write")
            return 0

        log.info(
            "Writing day grid to tab '%s' (%d days, ~%d employees)",
            worksheet_name, days_in_month,
            len(next(iter(cells_by_day.values()), {})),
        )

        # Column I = column index 9 (A=1, B=2, ...)
        DAY_GRID_START_COLUMN = 9
        DAY_GRID_HEADER_ROW = 1
        DAY_GRID_FIRST_DATA_ROW = 3
        # Last column = I + len(employees). For 13 employee columns, ends at V (22).
        max_emp_count = max((len(c) for c in cells_by_day.values()), default=0)
        end_column = DAY_GRID_START_COLUMN + max_emp_count  # I + N employees

        success_count = 0
        for day_num in range(1, days_in_month + 1):
            cells = cells_by_day.get(day_num, {})
            if not cells:
                continue

            # Plain integer keys - Zoho row 1 has the employee IDs as numbers
            data_map = {emp_id: value for emp_id, value in cells.items()}

            # Match by day number in the "IDs" column. Since cells in column I
            # are numeric, we send the value as an unquoted number.
            criteria = f'"{config.DAY_GRID_DAY_COLUMN}"={day_num}'

            row_for_day = DAY_GRID_FIRST_DATA_ROW + (day_num - 1)

            try:
                body = self._post(
                    resource_id,
                    "worksheet.records.update",
                    {
                        "worksheet_name": worksheet_name,
                        "header_row": 1,
                        "criteria": criteria,
                        "data": data_map,
                    },
                )
                affected = body.get("no_of_affected_rows", body.get("no_of_rows", "?"))
                log.info("  Day %2d updated: affected=%s", day_num, affected)
                success_count += 1
            except Exception as e:
                log.error("  Day %d update failed: %s", day_num, e)

        log.info(
            "Zoho Sheet: wrote day grid for %d/%d days in '%s'",
            success_count, days_in_month, worksheet_name,
        )
        return success_count
