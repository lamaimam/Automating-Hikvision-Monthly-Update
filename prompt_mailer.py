"""
Prompt-mailer - sends the day-18 email asking which day to deliver the sheet.

The email subject contains a unique cycle tag: [PAYROLL-PROMPT-YYYY-MM]
The IMAP reader uses this exact tag to find the reply, ignoring all other
mail in the inbox.

The body asks the user to reply with a single integer day-of-month (20-31)
on the FIRST LINE of the reply. The strict contract is what makes parsing
reliable - no regex against signatures, no quoted-history fragility.
"""
import logging
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage

import config

log = logging.getLogger(__name__)


def cycle_tag(today: date) -> str:
    """Unique subject tag for this month's prompt/reply pair."""
    return f"[PAYROLL-PROMPT-{today.year}-{today.month:02d}]"


def build_prompt_subject(today: date) -> str:
    return f"{cycle_tag(today)} Pick your send date"


def build_prompt_html(today: date, last_sent_iso: str) -> str:
    return f"""
    <div style="font-family: Arial, sans-serif; color: #1a1a1a;">
      <h2>Anwa Payroll - {today.strftime('%B %Y')}</h2>

      <p>Which day this month should the payroll sheet be sent?</p>

      <p><b>Reply to this email with a single number on the first line
      (between 20 and 31).</b></p>

      <p>Example: if you want the sheet on the 25th, reply with:</p>
      <pre style="background: #f4f4f4; padding: 10px; font-size: 18px;">25</pre>

      <p>The sheet will cover: <b>{last_sent_iso}</b> (day after last sheet)
      &rarr; the day you pick.</p>

      <hr>
      <p style="font-size: 12px; color: #666;">
        If you don't reply by the evening of day 19, no sheet will be sent
        this month and you'll need to investigate the scheduler logs.<br>
        Cycle tag: {cycle_tag(today)} (do not change the subject line of
        your reply).
      </p>
    </div>
    """


def send_prompt_email(today: date, last_sent_iso: str) -> None:
    """Send the prompt email via SMTP. Raises on failure."""
    msg = EmailMessage()
    msg["Subject"] = build_prompt_subject(today)
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = config.EMAIL_RECIPIENT
    msg.set_content(
        f"Reply with a single number (20-31) on the first line "
        f"to pick this month's send date. "
        f"Period covered: {last_sent_iso} (day after last sheet) to the day you pick."
    )
    msg.add_alternative(build_prompt_html(today, last_sent_iso), subtype="html")

    log.info(
        "Sending prompt email %r to %s",
        msg["Subject"], config.EMAIL_RECIPIENT,
    )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        config.SMTP_HOST,
        config.SMTP_PORT,
        context=context,
        timeout=30,
    ) as server:
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(msg)
    log.info("Prompt email sent.")


def send_parse_failure_email(today: date, raw_first_line: str) -> None:
    """Send a 'couldn't parse your reply' nag email."""
    subject = f"{cycle_tag(today)} Could not parse your reply"
    body_html = f"""
    <div style="font-family: Arial, sans-serif; color: #1a1a1a;">
      <h2>Payroll prompt - reply not understood</h2>
      <p>Your reply's first line was:</p>
      <pre style="background: #f4f4f4; padding: 10px;">{raw_first_line!r}</pre>
      <p>Expected a single integer between 20 and 31 on the first line.</p>
      <p>Please reply again to the original prompt email with just the day
      number, e.g. <code>25</code>.</p>
    </div>
    """

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = config.EMAIL_RECIPIENT
    msg.set_content(
        f"Could not parse your reply. First line was: {raw_first_line!r}. "
        f"Expected an integer between 20 and 31."
    )
    msg.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        config.SMTP_HOST,
        config.SMTP_PORT,
        context=context,
        timeout=30,
    ) as server:
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(msg)
    log.info("Parse-failure email sent.")


def send_no_reply_email(today: date) -> None:
    """Final nag if no reply was received by day 19 evening."""
    subject = f"{cycle_tag(today)} No reply received - no sheet this month"
    body_html = f"""
    <div style="font-family: Arial, sans-serif; color: #1a1a1a;">
      <h2>Payroll cycle - no reply received</h2>
      <p>The prompt for {today.strftime('%B %Y')} was sent but no valid reply
      was received by the evening of day 19.</p>
      <p>No payroll sheet will be sent automatically this month.</p>
      <p>To run manually: edit
      <code>files/scheduler_state.json</code> and set
      <code>current_cycle_reply_day</code> to the day you want, then wait
      for that day's launchd run (or trigger the script manually).</p>
    </div>
    """

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = config.EMAIL_RECIPIENT
    msg.set_content(
        "No valid reply was received for this month's payroll prompt. "
        "No sheet will be sent automatically."
    )
    msg.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        config.SMTP_HOST,
        config.SMTP_PORT,
        context=context,
        timeout=30,
    ) as server:
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(msg)
    log.info("No-reply nag email sent.")
