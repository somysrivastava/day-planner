from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.db.base import SessionLocal, engine

WEEKDAY_CODE_TO_APSCHEDULER = {
    "MO": "mon", "TU": "tue", "WE": "wed", "TH": "thu", "FR": "fri", "SA": "sat", "SU": "sun",
}

_scheduler: Optional[BackgroundScheduler] = None


def get_scheduler() -> BackgroundScheduler:
    """Singleton BackgroundScheduler, backed by the same Postgres DB via
    SQLAlchemyJobStore so scheduled jobs survive process restarts.

    Jobs are pickled for storage in that job store, which means: (1) job
    functions must be plain importable module-level functions - never a
    lambda, closure, or bound method; (2) job arguments must themselves be
    picklable (an item_id: int, never a Session or ORM object). Each job
    opens its own DB session when it actually fires, since the moment it
    fires could be long after (even a different process from) when it was
    scheduled."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(engine=engine)})
        _scheduler.start()
    return _scheduler


def _fire_nudge(item_id: int) -> None:
    from app.db.models import Item

    db = SessionLocal()
    try:
        item = db.get(Item, item_id)
        if item is None or item.status == "cancelled":
            return  # deleted/cancelled since this job was scheduled
        when = item.start_time.strftime("%I:%M%p").lstrip("0")
        print(f"[NUDGE] {item.title} starts at {when}")
        # TODO: real WhatsApp delivery once Week 3 wires up the messaging layer.
    finally:
        db.close()


def _fire_checkin(item_id: int) -> None:
    from app.db.models import Item

    db = SessionLocal()
    try:
        item = db.get(Item, item_id)
        if item is None or item.status in ("cancelled", "done"):
            return
        item.checkin_waiting = True
        db.commit()
        print(f"[CHECK-IN] Did you get to '{item.title}'?")
        # TODO: real WhatsApp delivery once Week 3 wires up the messaging layer.
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
