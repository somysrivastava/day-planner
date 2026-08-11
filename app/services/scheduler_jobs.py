from __future__ import annotations

import logging
from datetime import date as date_
from datetime import datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.db.base import SessionLocal, engine

logger = logging.getLogger(__name__)

WEEKDAY_CODE_TO_APSCHEDULER = {
    "MO": "mon", "TU": "tue", "WE": "wed", "TH": "thu", "FR": "fri", "SA": "sat", "SU": "sun",
}

_scheduler: Optional[BackgroundScheduler] = None

# APScheduler's own default misfire_grace_time is 1 second (verified against
# the installed package, see PROGRESS.md's Week 2 Day 4 notes) - meaning a
# job that's late by more than ~1 second is silently dropped (logged at
# WARNING, job.func() never called) rather than fired late. Found via a real
# leftover job in the job store getting dropped ~6 minutes overdue when a
# fresh process picked it up. Set generously here instead: nudges/check-ins
# are time-sensitive but not to-the-second critical - a nudge firing 20
# minutes late because the process briefly restarted is still useful; one
# firing 6+ hours late generally isn't. Applied via job_defaults (broadly,
# to every job on this scheduler), not per-job.
MISFIRE_GRACE_SECONDS = 60 * 60  # 1 hour


def get_scheduler() -> BackgroundScheduler:
    """Singleton BackgroundScheduler, backed by the same Postgres DB via
    SQLAlchemyJobStore so scheduled jobs survive process restarts.

    Jobs are pickled for storage in that job store, which means: (1) job
    functions must be plain importable module-level functions - never a
    lambda, closure, or bound method; (2) job arguments must themselves be
    picklable (an item_id: int, never a Session or ORM object). Each job
    opens its own DB session when it actually fires, since the moment it
    fires could be long after (even a different process from) when it was
    scheduled.

    A job still overdue beyond MISFIRE_GRACE_SECONDS is dropped, not caught
    up - see PROGRESS.md's Week 2 Day 4 notes for exactly what that means in
    production and what partial mitigation already exists for check-ins
    specifically (there is none for nudges)."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(engine=engine)},
            job_defaults={"misfire_grace_time": MISFIRE_GRACE_SECONDS},
        )
        _scheduler.start()
    return _scheduler


def _fire_nudge(item_id: int) -> None:
    from app.db.models import Item

    db = SessionLocal()
    try:
        try:
            item = db.get(Item, item_id)
            if item is None or item.status == "cancelled":
                return  # deleted/cancelled since this job was scheduled - expected, not a failure
            when = item.start_time.strftime("%I:%M%p").lstrip("0")
            print(f"[NUDGE] {item.title} starts at {when}")
            # TODO: real WhatsApp delivery once Week 3 wires up the messaging layer.
        except Exception:
            # Week 2 Day 4: APScheduler's own executor already logs any
            # uncaught exception from a job function (apscheduler.executors.
            # default, verified empirically), but with no context beyond a
            # generic job repr. Catching and re-raising here gets the
            # item_id into the log record before APScheduler's own handling
            # takes over.
            logger.error("Nudge fire FAILED for item_id=%s", item_id, exc_info=True)
            raise
    finally:
        db.close()


def _fire_checkin(item_id: int) -> None:
    from app.db.models import Item

    db = SessionLocal()
    try:
        try:
            item = db.get(Item, item_id)
            if item is None or item.status in ("cancelled", "done"):
                return
            item.checkin_waiting = True
            db.commit()
            print(f"[CHECK-IN] Did you get to '{item.title}'?")
            # TODO: real WhatsApp delivery once Week 3 wires up the messaging layer.
        except Exception:
            logger.error("Check-in fire FAILED for item_id=%s", item_id, exc_info=True)
            raise
    finally:
        db.close()


def cancel_job(job_id: Optional[str]) -> None:
    """Tolerant of a missing job - already fired (one-off DateTrigger jobs
    remove themselves after firing) or never existed."""
    if not job_id:
        return
    try:
        get_scheduler().remove_job(job_id)
    except JobLookupError:
        pass


def schedule_nudge(item_id: int, run_date: datetime, timezone: str) -> Optional[str]:
    """One-off nudge for a task/explicit-time item. Returns the job id, or
    None if run_date has already passed - nothing to schedule."""
    if run_date <= datetime.now(ZoneInfo(timezone)).replace(tzinfo=None):
        return None
    job_id = f"nudge-{item_id}"
    get_scheduler().add_job(
        _fire_nudge,
        trigger=DateTrigger(run_date=run_date, timezone=timezone),
        args=[item_id],
        id=job_id,
        replace_existing=True,
    )
    return job_id


def schedule_recurring_nudge(item_id: int, days: list[str], hour: int, minute: int, timezone: str) -> str:
    """Recurring nudge for a promoted recurring task / fixed event, matching
    its RRULE's BYDAY set via a CronTrigger."""
    job_id = f"nudge-{item_id}"
    day_of_week = ",".join(WEEKDAY_CODE_TO_APSCHEDULER[d] for d in days)
    get_scheduler().add_job(
        _fire_nudge,
        trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=timezone),
        args=[item_id],
        id=job_id,
        replace_existing=True,
    )
    return job_id


def schedule_checkin(item_id: int, run_date: datetime, timezone: str) -> Optional[str]:
    """Completion check-in, fired once the item's scheduled time has
    passed. Only called for important-flagged items."""
    if run_date <= datetime.now(ZoneInfo(timezone)).replace(tzinfo=None):
        return None
    job_id = f"checkin-{item_id}"
    get_scheduler().add_job(
        _fire_checkin,
        trigger=DateTrigger(run_date=run_date, timezone=timezone),
        args=[item_id],
        id=job_id,
        replace_existing=True,
    )
    return job_id


def schedule_evening_checkin(user_id: int, hour: int, minute: int, timezone: str) -> str:
    """Recurring daily evening check-in trigger for this user - one job per
    user (id 'evening-checkin-{user_id}'), not per item like nudge/checkin,
    since it's a once-nightly sweep of everything still pending that day,
    not tied to a single item's lifecycle."""
    job_id = f"evening-checkin-{user_id}"
    get_scheduler().add_job(
        _fire_evening_checkin,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
        args=[user_id],
        id=job_id,
        replace_existing=True,
    )
    return job_id


def _fire_evening_checkin(user_id: int) -> list[int]:
    """Sweeps today's task items whose scheduled time has already passed
    and are still status='pending', and flags each for a deferral answer.
    Deliberately excludes: items not yet due today (their window hasn't
    passed yet - not 'left over' yet, just not reached), and items with
    checkin_waiting=True (already mid the separate Day 5 important-task
    check-in flow - avoid asking about the same item through two different
    flows in one evening). Returns the flagged item ids - useful for tests/
    the CLI, not just the print-based delivery stub."""
    from app.db.models import Item, User

    db = SessionLocal()
    try:
        try:
            user = db.get(User, user_id)
            if user is None:
                logger.warning("Evening check-in fired for user_id=%s but that user no longer exists", user_id)
                return []
            tz = ZoneInfo(user.timezone)
            now_local = datetime.now(tz).replace(tzinfo=None)
            today_local = now_local.date()

            candidates = (
                db.query(Item)
                .filter(
                    Item.user_id == user_id,
                    Item.type == "task",
                    Item.status == "pending",
                    Item.checkin_waiting.is_(False),
                    Item.start_time.isnot(None),
                    Item.end_time.isnot(None),
                )
                .all()
            )
            pending_today = [
                item
                for item in candidates
                if item.start_time.astimezone(tz).replace(tzinfo=None).date() == today_local
                and item.end_time.astimezone(tz).replace(tzinfo=None) <= now_local
            ]

            if not pending_today:
                print("[EVENING CHECK-IN] nothing left over today - skipping silently, no message sent")
                return []

            for item in pending_today:
                item.evening_checkin_flagged = True
            db.commit()

            titles = ", ".join(f"'{i.title}'" for i in pending_today)
            print(
                f"[EVENING CHECK-IN] Anything left over from today you want on tomorrow's list? "
                f"Still pending: {titles}"
            )
            return [i.id for i in pending_today]
        except Exception:
            logger.error("Evening check-in fire FAILED for user_id=%s", user_id, exc_info=True)
            raise
    finally:
        db.close()


def answer_evening_checkin(
    db,
    item_id: int,
    choice: Literal["tomorrow", "choose_date", "keep_pending"],
    chosen_date: Optional[date_] = None,
):
    """Answers one item from a flagged evening check-in. 'tomorrow'/
    'choose_date' reuse orchestrator.reschedule_confirmed_item (same
    mechanism as the completion check-in's 'no' answer, per spec) - just
    with an explicit target_date instead of letting it default to the
    item's own (already-past) date. 'keep_pending' just clears the flag
    and leaves the item exactly where it is - the spec doesn't define a
    separate 'missed' state, so an item left this way simply sits as
    status='pending' at its original past time until the user brings it up
    again (see PROGRESS.md's Day 7 known-limitations note)."""
    from app.db.models import Item, User

    item = db.get(Item, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")
    if not item.evening_checkin_flagged:
        raise ValueError(f"Item {item_id} has no evening check-in currently waiting for an answer")

    item.evening_checkin_flagged = False
    db.commit()

    if choice == "keep_pending":
        return None

    from app.services.orchestrator import reschedule_confirmed_item  # local import breaks the circular dependency

    if choice == "tomorrow":
        user = db.get(User, item.user_id)
        tz = ZoneInfo(user.timezone)
        target = item.start_time.astimezone(tz).replace(tzinfo=None).date() + timedelta(days=1)
    elif choice == "choose_date":
        if chosen_date is None:
            raise ValueError("chosen_date is required when choice='choose_date'")
        target = chosen_date
    else:
        raise ValueError(f"Unknown choice {choice!r}")

    return reschedule_confirmed_item(db, item_id, target_date=target)


def answer_checkin(db, item_id: int, done: bool):
    """Simulates the user's reply to a completion check-in (stands in for
    a real WhatsApp reply until Week 3). 'Yes' just acknowledges and stops
    - no follow-up, no scorekeeping, per the spec's non-nagging tone. 'No'
    reuses the shared reschedule mechanism (orchestrator.
    reschedule_confirmed_item) rather than a second path, per the spec."""
    from app.db.models import Item

    item = db.get(Item, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")
    if not item.checkin_waiting:
        raise ValueError(f"Item {item_id} has no check-in currently waiting for an answer")

    item.checkin_waiting = False

    if done:
        item.status = "done"
        db.commit()
        return None

    db.commit()
    from app.services.orchestrator import reschedule_confirmed_item  # local import breaks the circular dependency

    return reschedule_confirmed_item(db, item_id)
