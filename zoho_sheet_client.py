"""
Zoho Sheet client - reads employee contracts and appends report rows.

API reference: https://www.zoho.com/sheet/help/api/v2/

Two operations needed:
  1. Read contracts from the input sheet (employee id, name, OT rate, etc.)
  2. Append calculated rows to the master report sheet.
"""
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

import config
from zoho_auth import ZohoAuth

log = logging.getLogger(__name__)


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
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Zoho Sheet {method} failed: {body}")
        return body

    def read_contracts(self) -> List[EmployeeContract]:
        """Read employee contract rows from the configured contracts sheet."""
        body = self._post(
            config.ZOHO_SHEET_CONTRACTS_ID,
            "worksheet.records.fetch",
            {
                "worksheet_name": config.ZOHO_SHEET_CONTRACTS_WORKSHEET,
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

    def append_report_rows(self, rows: List[Dict[str, str]]) -> None:
        """Append calculated payroll rows to the master report sheet.

        Each row is a dict mapping column name -> value (stringified).
        """
        if not rows:
            log.info("No rows to append to master report")
            return

        self._post(
            config.ZOHO_SHEET_MASTER_REPORT_ID,
            "worksheet.records.add",
            {
                "worksheet_name": config.ZOHO_SHEET_MASTER_REPORT_WORKSHEET,
                "header_row": 1,
                "json_data": rows,
            },
        )
        log.info("Zoho Sheet: appended %d rows to master report", len(rows))
