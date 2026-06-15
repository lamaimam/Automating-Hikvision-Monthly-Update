"""
Scheduler entry point - email-prompt state machine.

Invoked by launchd on three specific events per cycle:
  - Day 18, 9:00 AM:  send the prompt email (action: send_prompt)
  - Day 18, 8:00 PM:  check IMAP for the reply (action: check_reply)
  - Day 19, 8:00 PM:  fallback IMAP check       (action: check_reply)
  - Chosen day, 9 AM: run the pipeline           (action: run_pipeline)

The script reads scheduler_state.json to decide what to do on each run.
It is idempotent: running send_prompt twice doesn't send two emails;
running check_reply after a reply is already parsed is a no-op.

Action is inferred from today's date + state, OR can be overridden via
the --action CLI flag (useful for testing).

First-run / catch-up: if invoked between day 18 and day 31 and no prompt
has been sent yet this cycle, it sends the prompt immediately regardless
of the time of day. This handles the case where the system is installed
mid-cycle.
"""
import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# Make imports work when launchd runs us from outside the project dir
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import config
import imap_reader
import prompt_mailer
import scheduler_state
from prompt_mailer import cycle_tag


def setup_logging() -> logging.Logger:
    log_dir = SCRIPT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"scheduler-{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("scheduler")


def _ensure_current_cycle(state: dict, today: date, log: logging.Logger) -> dict:
    """Start a new cycle in state if today's month differs from state's cycle."""
    if not scheduler_state.is_current_cycle(state, today):
        log.info(
            "Starting new cycle for %d-%02d (was %s-%s)",
            today.year, today.month,
            state.get("current_cycle_year"),
            state.get("current_cycle_month"),
        )
        state = scheduler_state.start_new_cycle(state, today)
        scheduler_state.save_state(state)
    return state


def action_send_prompt(state: dict, today: date, log: logging.Logger) -> int:
    """Send the day-18 prompt email. Idempotent."""
    state = _ensure_current_cycle(state, today, log)

    if state.get("current_cycle_prompt_sent"):
        log.info(
            "Prompt already sent on %s for cycle %d-%02d - skipping",
            state["current_cycle_prompt_sent"],
            state["current_cycle_year"], state["current_cycle_month"],
        )
        return 0

    last_sent = scheduler_state.get_last_sent_date(state)
    if last_sent is None:
        log.error(
            "No last_sent_date in state. Edit files/scheduler_state.json "
            "to seed it before sending the prompt."
        )
        return 1

    period_start_iso = (last_sent.fromordinal(last_sent.toordinal() + 1)).isoformat()

    try:
        prompt_mailer.send_prompt_email(today, period_start_iso)
    except Exception as e:
        log.exception("Failed to send prompt email: %s", e)
        return 1

    state["current_cycle_prompt_sent"] = today.isoformat()
    scheduler_state.save_state(state)
    log.info("Prompt sent and state updated.")
    return 0


def action_check_reply(state: dict, today: date, log: logging.Logger) -> int:
    """Check IMAP for a reply and update state if one is found."""
    state = _ensure_current_cycle(state, today, log)

    if state.get("current_cycle_reply_day") is not None:
        log.info(
            "Reply already recorded for cycle (day=%d) - skipping IMAP check",
            state["current_cycle_reply_day"],
        )
        return 0

    if not state.get("current_cycle_prompt_sent"):
        log.warning(
            "No prompt was sent for this cycle - skipping reply check"
        )
        return 0

    tag = cycle_tag(today)
    day, first_line = imap_reader.fetch_reply_day(tag, today)

    if day is not None:
        state["current_cycle_reply_day"] = day
        scheduler_state.save_state(state)
        log.info(
            "Reply parsed: send date for this cycle = %d-%02d-%02d",
            today.year, today.month, day,
        )
        return 0

    if first_line is not None:
        # Reply found but unparseable - nag the user
        log.warning("Sending parse-failure email")
        try:
            prompt_mailer.send_parse_failure_email(today, first_line)
        except Exception as e:
            log.exception("Failed to send parse-failure email: %s", e)
        return 0

    # No reply at all
    log.info("No reply found yet")

    # If today is day 19 (the fallback check day) and still no reply,
    # send the no-reply nag email so the user knows the cycle is dead.
    if today.day == 19:
        log.warning(
            "Day 19 fallback check produced no reply - sending no-reply nag"
        )
        try:
            prompt_mailer.send_no_reply_email(today)
        except Exception as e:
            log.exception("Failed to send no-reply email: %s", e)
    return 0


def action_run_pipeline(state: dict, today: date, log: logging.Logger) -> int:
    """Run the full payroll pipeline for the rolling period."""
    chosen = scheduler_state.get_chosen_send_date(state)
    if chosen is None:
        log.error(
            "No chosen send date in state - cannot run pipeline. "
            "Did the reply check succeed?"
        )
        return 1

    if today != chosen:
        # Only enforce the date guard for the CURRENT month - that's where
        # launchd auto-firing on the wrong day would be a real bug. For
        # backfill runs (state intentionally set to a past month), let
        # the pipeline proceed regardless of today's date.
        if chosen.year == today.year and chosen.month == today.month:
            log.error(
                "run_pipeline invoked on %s but chosen date is %s - refusing",
                today.isoformat(), chosen.isoformat(),
            )
            return 1
        log.warning(
            "Backfill mode detected: chosen date %s is in a different month "
            "than today (%s) - proceeding",
            chosen.isoformat(), today.isoformat(),
        )

    if state.get("current_cycle_done"):
        log.info("Pipeline already ran for this cycle - skipping")
        return 0

    last_sent = scheduler_state.get_last_sent_date(state)
    if last_sent is None:
        log.error(
            "No last_sent_date in state - cannot compute period start"
        )
        return 1

    period_start = last_sent.fromordinal(last_sent.toordinal() + 1)
    period_end = chosen

    log.info(
        "Running pipeline for period %s -> %s",
        period_start.isoformat(), period_end.isoformat(),
    )

    try:
        import main as main_module
    except ImportError as e:
        log.exception("Could not import main.py: %s", e)
        return 1

    if not hasattr(main_module, "run_period"):
        log.error("main.py is missing run_period(start, end)")
        return 1

    try:
        rc = main_module.run_period(period_start, period_end)
    except Exception as e:
        log.exception("Pipeline raised an exception: %s", e)
        return 1

    if rc != 0:
        log.error("Pipeline returned non-zero exit code: %s", rc)
        return rc

    # Success - update state
    state["last_sent_date"] = period_end.isoformat()
    state["current_cycle_done"] = True
    scheduler_state.save_state(state)
    log.info("Pipeline completed. last_sent_date updated to %s",
             period_end.isoformat())
    return 0


def infer_action(state: dict, today: date, log: logging.Logger) -> str:
    """Decide what action to take based on today's date and state.

    Rules:
      - If pipeline is done for current cycle and today is still same month -> noop
      - If today is the chosen send date -> run_pipeline
      - If today.day == 18 and no prompt sent -> send_prompt
      - If today.day in {18, 19} and prompt sent but no reply -> check_reply
      - Catch-up: if today.day in [19, 31] and no prompt sent yet for current
        cycle -> send_prompt now (handles install mid-cycle)
      - Catch-up: if today.day in [19, 31] and prompt sent but no reply ->
        check_reply now
      - Otherwise -> noop
    """
    # If today matches the chosen send date for this cycle, run pipeline.
    chosen = scheduler_state.get_chosen_send_date(state)
    if (
        chosen == today
        and scheduler_state.is_current_cycle(state, today)
        and not state.get("current_cycle_done")
    ):
        return "run_pipeline"

    if today.day < 18:
        return "noop"

    # Day 18-31 territory
    in_cycle = scheduler_state.is_current_cycle(state, today)
    prompt_sent = in_cycle and bool(state.get("current_cycle_prompt_sent"))
    reply_in = in_cycle and state.get("current_cycle_reply_day") is not None

    if not prompt_sent:
        return "send_prompt"
    if not reply_in:
        return "check_reply"
    return "noop"


def main() -> int:
    parser = argparse.ArgumentParser(description="Anwa payroll scheduler")
    parser.add_argument(
        "--action",
        choices=["send_prompt", "check_reply", "run_pipeline", "auto"],
        default="auto",
        help="Force a specific action. Default 'auto' infers from date+state.",
    )
    parser.add_argument(
        "--seed-last-sent",
        help="ISO date to seed last_sent_date if state file doesn't exist "
             "(e.g. 2026-04-25 for first deployment).",
    )
    args = parser.parse_args()

    log = setup_logging()
    today = date.today()
    log.info("=" * 60)
    log.info("Scheduler invoked at %s, today=%s, requested_action=%s",
             datetime.now().isoformat(timespec="seconds"),
             today.isoformat(), args.action)
    log.info("=" * 60)

    seed = date.fromisoformat(args.seed_last_sent) if args.seed_last_sent else None
    state = scheduler_state.load_state(seed_last_sent=seed)
    log.info("Loaded state: %s", state)

    action = args.action
    if action == "auto":
        action = infer_action(state, today, log)
        log.info("Inferred action: %s", action)

    if action == "noop":
        log.info("Nothing to do today.")
        return 0
    if action == "send_prompt":
        return action_send_prompt(state, today, log)
    if action == "check_reply":
        return action_check_reply(state, today, log)
    if action == "run_pipeline":
        return action_run_pipeline(state, today, log)

    log.error("Unknown action: %s", action)
    return 1


if __name__ == "__main__":
    sys.exit(main())