"""
Email sender using Zoho Mail API.

API reference: https://www.zoho.com/mail/help/api/post-send-email.html
Endpoint: POST /api/accounts/{account_id}/messages
"""
import logging
from typing import List

import requests

import config
from zoho_auth import ZohoAuth

log = logging.getLogger(__name__)


def send_report_email(
    subject: str,
    body_html: str,
    recipient: str = config.EMAIL_RECIPIENT,
) -> None:
    """Send the payroll report notification email."""
    url = f"{config.ZOHO_MAIL_BASE}/accounts/{config.EMAIL_SENDER_ACCOUNT_ID}/messages"
    payload = {
        "fromAddress": config.EMAIL_SENDER,
        "toAddress": recipient,
        "subject": subject,
        "content": body_html,
        "mailFormat": "html",
    }
    resp = requests.post(
        url,
        headers={
            **ZohoAuth.auth_header(),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    log.info("Email sent to %s: %s", recipient, subject)


def build_summary_html(
    year: int,
    month: int,
    employee_count: int,
    review_items: List[str],
    sheet_url: str,
) -> str:
    """Render the email body."""
    review_block = ""
    if review_items:
        items = "".join(f"<li>{item}</li>" for item in review_items)
        review_block = f"""
        <h3>Manual Review Required</h3>
        <ul>{items}</ul>
        """

    return f"""
    <div style="font-family: Arial, sans-serif; color: #1a1a1a;">
      <h2>Payroll Attendance Report - {year}-{month:02d}</h2>
      <p>The attendance-overtime reconciliation has been appended to the
      master sheet.</p>
      <p><b>Employees processed:</b> {employee_count}</p>
      <p><b>Master sheet:</b> <a href="{sheet_url}">Open in Zoho Sheet</a></p>
      {review_block}
      <hr>
      <p style="font-size: 12px; color: #666;">
        Generated automatically by the Company Org. payroll reconciliation
        script. Verify totals against the contracts sheet before WPS submission.
      </p>
    </div>
    """
