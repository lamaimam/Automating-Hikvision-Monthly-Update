"""
Email sender using plain SMTP.

We switched from the Zoho Mail REST API to SMTP because the API was returning
opaque 500 "Internal Error" responses with no actionable info. SMTP error
messages are much more diagnosable.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import List

import config
log = logging.getLogger(__name__)


def send_report_email(subject: str, body_html: str) -> None:
    """Send the payroll report email via Zoho SMTP."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = config.EMAIL_RECIPIENT
    msg.set_content(
        "This email contains an HTML payroll summary. "
        "If you cannot see it, please switch to an HTML-capable mail client."
    )
    msg.add_alternative(body_html, subtype="html")

    log.info(
        "Sending email via SMTP %s:%d -> %s",
        config.SMTP_HOST, config.SMTP_PORT, config.EMAIL_RECIPIENT,
    )

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(
            config.SMTP_HOST,
            config.SMTP_PORT,
            context=context,
            timeout=30,
        ) as server:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(msg)
        log.info("Email sent successfully to %s", config.EMAIL_RECIPIENT)
    except smtplib.SMTPAuthenticationError as e:
        log.error("SMTP authentication failed: %s", e)
        log.error(
            "Check that SMTP_APP_PASSWORD is a valid app password "
            "(not your regular Zoho login password) for %s",
            config.SMTP_USERNAME,
        )
        raise
    except smtplib.SMTPException as e:
        log.error("SMTP send failed: %s", e)
        raise


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
        Generated automatically by the Anwa BioSciences payroll reconciliation
        script. Verify totals against the contracts sheet before WPS submission.
      </p>
    </div>
    """