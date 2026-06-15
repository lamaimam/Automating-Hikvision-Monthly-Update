"""
Scheduler state file - tracks where the monthly cycle stands across runs.

Lives at: <project>/files/scheduler_state.json

The file is the entire memory of the scheduler. On every launchd-triggered
run, scheduler.py reads this file, decides what action to take, and writes
the file back.

Schema:
{
    "last_sent_date":            "2026-04-25",   # ISO date of the last sheet sent
    "current_cycle_year":        2026,
    "current_cycle_month":       5,
    "current_cycle_prompt_sent": "2026-05-21",   # ISO date prompt email went out
    "current_cycle_reply_day":   null,           # integer day-of-month, or null
    "current_cycle_done":        false           # has the pipeline run for this cycle?
}

States the scheduler can be in:
  A) NO PROMPT SENT YET this cycle  -> prompt_sent is null
  B) PROMPT SENT, NO REPLY          -> prompt_sent set, reply_day is null
  C) REPLY RECEIVED, AWAITING DATE  -> reply_day set, done is false
  D) CYCLE DONE                     -> done is true, waiting for next month

Transitions:
  - On day 18 morning, state A -> state B (prompt sent)
  - On day 18 evening or day 19 evening, state B -> state C (reply parsed)
  - On the chosen day, state C -> state D (pipeline runs, sheet sent)
  - On day 18 of the next month, state D -> state A (new cycle)
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = SCRIPT_DIR / "files" / "scheduler_state.json"


def _empty_state(last_sent: Optional[date] = None) -> dict:
    return {
        "last_sent_date": last_sent.isoformat() if last_sent else None,
        "current_cycle_year": None,
        "current_cycle_month": None,
        "current_cycle_prompt_sent": None,
        "current_cycle_reply_day": None,
        "current_cycle_done": False,
    }


def load_state(seed_last_sent: Optional[date] = None) -> dict:
    """Load state from disk. If file doesn't exist, create it with a seed.

    seed_last_sent is used only on first run when the file doesn't exist.
    """
    if not STATE_FILE.exists():
        log.warning(
            "State file %s does not exist - creating with seed last_sent=%s",
            STATE_FILE, seed_last_sent,
        )
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = _empty_state(seed_last_sent)
        save_state(state)
        return state

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    return state


def save_state(state: dict) -> None:
    """Write state back to disk atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)
    log.debug("Saved scheduler state: %s", state)


def start_new_cycle(state: dict, today: date) -> dict:
    """Reset cycle-specific fields for a new month. Preserves last_sent_date."""
    state["current_cycle_year"] = today.year
    state["current_cycle_month"] = today.month
    state["current_cycle_prompt_sent"] = None
    state["current_cycle_reply_day"] = None
    state["current_cycle_done"] = False
    return state


def is_current_cycle(state: dict, today: date) -> bool:
    """True if the state file's cycle fields refer to today's month."""
    return (
        state.get("current_cycle_year") == today.year
        and state.get("current_cycle_month") == today.month
    )


def get_last_sent_date(state: dict) -> Optional[date]:
    raw = state.get("last_sent_date")
    return date.fromisoformat(raw) if raw else None


def get_chosen_send_date(state: dict) -> Optional[date]:
    """The full date the user picked for this cycle, or None if no reply yet."""
    day = state.get("current_cycle_reply_day")
    year = state.get("current_cycle_year")
    month = state.get("current_cycle_month")
    if not (day and year and month):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        log.error(
            "Invalid chosen date in state: year=%s month=%s day=%s",
            year, month, day,
        )
        return None
