"""
IMAP reader - fetches the user's reply to the day-18 prompt email.

Connects to Zoho Mail over IMAP using an app-specific password (same
credential pattern as the SMTP setup, but a separate IMAP password may
be needed - generate one at:
   https://accounts.zoho.com/home#security/app_passwords

Strategy:
  1. Connect to IMAP, log in, select INBOX.
  2. Search for unread (or read - we don't care) messages whose subject
     contains the cycle tag, e.g. '[PAYROLL-PROMPT-2026-05]'.
  3. Fetch the most recent matching message.
  4. Extract the plain-text body. (Multipart replies have text/plain and
     text/html parts; we use text/plain.)
  5. Take the FIRST LINE of the body, strip whitespace, parse as integer.
  6. Validate the integer is between 20 and 31 AND a valid day in the
     current month (e.g. 31 rejected for February).
  7. Return the validated integer, or None if anything failed.

Strictness is the point: the prompt email tells the user explicitly that
the FIRST LINE is parsed. This means no regex against signatures, no
quoted-history fragility, no guessing what they meant.
"""
import calendar
import email
import email.policy
import imaplib
import logging
from datetime import date
from email.message import Message
from typing import Optional, Tuple

import config

log = logging.getLogger(__name__)


def _first_text_part(msg: Message) -> str:
    """Extract the text/plain content from a (possibly multipart) message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_content()
                except (LookupError, UnicodeDecodeError):
                    # Fall back to raw bytes decode
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode("utf-8", errors="replace")
        # No text/plain - fall back to first text/html stripped of tags
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_content()
                except (LookupError, UnicodeDecodeError):
                    payload = part.get_payload(decode=True) or b""
                    html = payload.decode("utf-8", errors="replace")
                # Crude: strip tags. Good enough since we only need first line.
                import re
                return re.sub(r"<[^>]+>", "", html)
        return ""
    try:
        return msg.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = msg.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _parse_first_line_as_day(body_text: str, today: date) -> Tuple[Optional[int], str]:
    """Parse the first non-empty line as an integer day-of-month.

    Returns (day_or_None, raw_first_line_for_logging).
    """
    first_line = ""
    for line in body_text.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if not first_line:
        return None, "<empty body>"

    # Tolerate "20.", "20)", "Day 20", just-the-number etc. by extracting
    # the first integer in the first line.
    import re
    match = re.search(r"\b(\d{1,2})\b", first_line)
    if not match:
        return None, first_line

    try:
        day = int(match.group(1))
    except ValueError:
        return None, first_line

    # Must be in valid send-day range
    if not (20 <= day <= 31):
        log.warning(
            "Parsed day %d is outside allowed range 20-31 (first line: %r)",
            day, first_line,
        )
        return None, first_line

    # Must be a valid day in the current month
    _, last_day_of_month = calendar.monthrange(today.year, today.month)
    if day > last_day_of_month:
        log.warning(
            "Parsed day %d does not exist in %d-%02d (max is %d)",
            day, today.year, today.month, last_day_of_month,
        )
        return None, first_line

    return day, first_line


def fetch_reply_day(cycle_tag: str, today: date) -> Tuple[Optional[int], Optional[str]]:
    """Check IMAP for a reply to the prompt and parse the chosen day.

    Returns (day_or_None, raw_first_line_or_None).
      - (day, None)            -> success, valid reply found and parsed
      - (None, first_line)     -> reply found but unparseable (caller can nag)
      - (None, None)           -> no reply found at all
    """
    log.info(
        "IMAP: connecting to %s:%d as %s",
        config.IMAP_HOST, config.IMAP_PORT, config.IMAP_USERNAME,
    )

    try:
        mailbox = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=30)
    except Exception as e:
        log.error("IMAP connection failed: %s", e)
        return None, None

    try:
        mailbox.login(config.IMAP_USERNAME, config.IMAP_PASSWORD)
    except imaplib.IMAP4.error as e:
        log.error(
            "IMAP login failed: %s. Check that IMAP_PASSWORD is a valid "
            "Zoho app-specific password.", e,
        )
        try:
            mailbox.logout()
        except Exception:
            pass
        return None, None

    try:
        mailbox.select("INBOX")

        # Search: messages whose subject contains the cycle tag.
        # IMAP SEARCH syntax: SUBJECT "<text>"
        # We don't filter by read/unread - if the user re-replied, the latest
        # message wins. We do skip messages we sent ourselves (the original
        # prompt has the same tag in its subject).
        search_query = f'(SUBJECT "{cycle_tag}")'
        log.info("IMAP: searching %r", search_query)
        typ, msgnums = mailbox.search(None, search_query)
        if typ != "OK":
            log.error("IMAP SEARCH failed: %s", typ)
            return None, None

        ids = msgnums[0].split() if msgnums and msgnums[0] else []
        if not ids:
            log.info("IMAP: no messages found with subject containing %r", cycle_tag)
            return None, None

        log.info("IMAP: found %d matching message(s): %s", len(ids), ids)

        # Walk from newest to oldest. We need to distinguish the original
        # prompt (sent by us) from the user's reply. Filtering by From
        # doesn't work in self-to-self setups (sender == recipient ==
        # same Zoho account). The reliable discriminator is the "Re:"
        # prefix that mail clients add to reply subjects, and/or the
        # presence of an In-Reply-To / References header (set by clients
        # when you hit Reply, never on a fresh compose).
        for msg_id in reversed(ids):
            typ, data = mailbox.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                log.warning("IMAP: could not fetch message %s", msg_id)
                continue

            raw_bytes = data[0][1]
            msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

            subject = (msg.get("Subject") or "").strip()
            in_reply_to = msg.get("In-Reply-To")
            references = msg.get("References")
            from_header = (msg.get("From") or "").lower()

            # A reply has either a "Re:" / "RE:" / "Fwd:" prefix, or
            # In-Reply-To / References headers pointing at a parent message.
            looks_like_reply = (
                subject.lower().startswith(("re:", "fwd:", "fw:"))
                or in_reply_to is not None
                or references is not None
            )

            if not looks_like_reply:
                log.info(
                    "IMAP: msg %s is the original prompt (subject=%r, "
                    "no reply headers), skipping",
                    msg_id, subject,
                )
                continue

            log.info(
                "IMAP: parsing reply from %s, subject=%r",
                from_header, subject,
            )

            body = _first_text_part(msg)
            day, first_line = _parse_first_line_as_day(body, today)

            if day is not None:
                log.info("IMAP: parsed reply day = %d", day)
                return day, first_line
            else:
                log.warning(
                    "IMAP: reply found but unparseable, first line: %r",
                    first_line,
                )
                return None, first_line

        log.info("IMAP: no reply messages found in matching set (only original prompt)")
        return None, None

    finally:
        try:
            mailbox.close()
        except Exception:
            pass
        try:
            mailbox.logout()
        except Exception:
            pass
