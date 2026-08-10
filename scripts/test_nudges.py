import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402
from app.services import scheduler_jobs  # noqa: E402
from app.services.orchestrator import build_plan, confirm_plan  # noqa: E402
from app.services.parser import ExplicitTimeItem, ParsedMessage, parse_message  # noqa: E402
from scripts.test_scheduler import cleanup_test_data  # noqa: E402

YOU = "+919354211791"
SPARSE = "+910000000001"
PACKED = "+910000000002"
NO_MORNING = "+910000000003"


def user_id(db, phone):
    return db.query(User).filter(User.phone_number == phone).first().id


def cleanup_nudge_test_data():
    """Reuses test_scheduler.py's cleanup (tasks/promoted-recurring events/
    overrides) and additionally clears apscheduler_jobs - a persistent job
    store means leftover jobs from a previous run can fire mid-test and
    contaminate output otherwise (this bit us once - see PROGRESS.md)."""
    db = SessionLocal()
    cleanup_test_data(db)
    db.execute(text("DELETE FROM apscheduler_jobs"))
    db.commit()
    db.close()


def show_apscheduler_jobs(db):
    rows = db.execute(text("SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time")).fetchall()
    for job_id, next_run_time in rows:
        when = datetime.fromtimestamp(next_run_time).strftime("%Y-%m-%d %H:%M:%S") if next_run_time else "?"
        print(f"  job={job_id}  next_run={when}")


def job_persistence_multi_user():
    print("=" * 70)
    print("SCENARIO A: nudge job persistence across seed users (future dates, not real-time)")
    print("=" * 70)
    db = SessionLocal()
    scheduler_jobs.get_scheduler()  # ensure apscheduler_jobs table exists

    # A fresh date not used by earlier test_scheduler.py runs, avoiding collisions.
    target = date(2026, 8, 27)  # Thursday

    for phone, label in [(YOU, "real user"), (PACKED, "tightly packed")]:
        uid = user_id(db, phone)
        parsed = parse_message("prep slides, 30 minutes", reference_date=target)
        plan = build_plan(db, uid, target, parsed)
        if plan.pending_duration_questions:
            from app.services.orchestrator import resolve_duration_question

            resolve_duration_question(db, plan, plan.pending_duration_questions[0], 30)
        saved = confirm_plan(db, plan)
        row = saved[0]
        print(f"\n{label} ({phone}): saved id={row.id} start={row.start_time} nudge_job_id={row.nudge_job_id}")
        assert row.nudge_job_id is not None, "nudge should have been scheduled (start_time is far in the future)"

    print("\nAll persisted jobs currently in apscheduler_jobs:")
    show_apscheduler_jobs(db)
    db.close()


def recurring_fixed_event_nudge():
    print("\n" + "=" * 70)
    print("SCENARIO B: recurring nudge (CronTrigger) for a promoted recurring task")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, NO_MORNING)
    target = date(2026, 8, 25)  # Tuesday

    parsed = parse_message("stretch for 10 minutes every Tue/Thu at 7am", reference_date=target)
    plan = build_plan(db, uid, target, parsed)
    saved = confirm_plan(db, plan)
    row = saved[0]
    print(f"saved id={row.id} title={row.title!r} rrule={row.recurrence_rule!r} nudge_job_id={row.nudge_job_id}")

    job = scheduler_jobs.get_scheduler().get_job(row.nudge_job_id)
    print(f"trigger type: {type(job.trigger).__name__}  ({job.trigger})")
    assert type(job.trigger).__name__ == "CronTrigger", "recurring fixed event should get a CronTrigger, not DateTrigger"

    db.close()


def live_fire_proof():
    print("\n" + "=" * 70)
    print("SCENARIO C+D: LIVE FIRE - real near-future jobs actually firing (~3-4 min wait)")
    print("=" * 70)
    db = SessionLocal()

    now = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)  # next clean minute boundary

    # --- C: plain nudge, SPARSE user ---
    sparse_uid = user_id(db, SPARSE)
    sparse = db.get(User, sparse_uid)
    sparse.nudge_lead_minutes = 1  # temporarily short, restored at the end
    db.commit()

    start_c = now + timedelta(minutes=2)
    item_c = ExplicitTimeItem(
        type="explicit_time_item", title="Live nudge test", date=start_c.date().isoformat(),
        start_time=start_c.strftime("%H:%M"), duration_minutes=1, important=False,
    )
    plan_c = build_plan(db, sparse_uid, start_c.date(), ParsedMessage(items=[item_c]))
    saved_c = confirm_plan(db, plan_c)[0]
    print(f"[C] confirmed id={saved_c.id} start={saved_c.start_time} nudge fires ~{start_c - timedelta(minutes=1)}")

    # --- D: important task, NO_MORNING user - full check-in cycle ---
    nm_uid = user_id(db, NO_MORNING)
    no_morning = db.get(User, nm_uid)
    no_morning.nudge_lead_minutes = 1
    db.commit()

    start_d = now + timedelta(minutes=2)
    item_d = ExplicitTimeItem(
        type="explicit_time_item", title="Live checkin test", date=start_d.date().isoformat(),
        start_time=start_d.strftime("%H:%M"), duration_minutes=1, important=True,
    )
    plan_d = build_plan(db, nm_uid, start_d.date(), ParsedMessage(items=[item_d]))
    saved_d = confirm_plan(db, plan_d)[0]
    old_nudge_job_id, old_checkin_job_id = saved_d.nudge_job_id, saved_d.checkin_job_id
    old_start = saved_d.start_time
    print(f"[D] confirmed id={saved_d.id} start={saved_d.start_time} important=True")
    print(f"    nudge_job_id={old_nudge_job_id}  checkin_job_id={old_checkin_job_id}")

    db.close()

    print(f"\nWaiting for both nudges to fire (~{(start_c - timedelta(minutes=1) - datetime.now()).seconds}s)...")
    time.sleep(max(0, (start_c - timedelta(minutes=1) - datetime.now()).total_seconds()) + 5)

    print(f"Waiting for the check-in to fire (~{(start_d + timedelta(minutes=1) - datetime.now()).seconds}s)...")
    time.sleep(max(0, (start_d + timedelta(minutes=1) - datetime.now()).total_seconds()) + 5)

    db = SessionLocal()
    refreshed_d = db.get(Item, saved_d.id)
    print(f"\n[D] checkin_waiting after firing: {refreshed_d.checkin_waiting} (should be True)")
    assert refreshed_d.checkin_waiting is True, "check-in job should have fired and set checkin_waiting"

    print("Simulating the WhatsApp reply: 'no, didn't get to it'")
    no_fit = scheduler_jobs.answer_checkin(db, saved_d.id, done=False)
    print(f"answer_checkin result (None = successfully rescheduled): {no_fit}")

    db.close()
    db = SessionLocal()
    rescheduled = db.get(Item, saved_d.id)
    print(f"\n[D] after reschedule: start={rescheduled.start_time} (was {old_start}) important={rescheduled.important}")
    print(f"    nudge_job_id={rescheduled.nudge_job_id} (was {old_nudge_job_id})")
    print(f"    checkin_job_id={rescheduled.checkin_job_id} (was {old_checkin_job_id})")
    assert rescheduled.important is True, "important must survive the reschedule - this is the bug confirm_plan had"
    assert rescheduled.checkin_job_id is not None, "a check-in job should be scheduled for the new time"
    # Job ids are deterministic per-item (f"checkin-{item_id}"), reused via
    # replace_existing=True - so the id staying the same across a reschedule
    # is correct, not stale. What actually proves it's live is next_run_time
    # matching the NEW end_time, not the id changing.
    job = scheduler_jobs.get_scheduler().get_job(rescheduled.checkin_job_id)
    assert job.next_run_time.replace(tzinfo=None) == rescheduled.end_time.replace(tzinfo=None), (
        f"check-in job's next_run_time ({job.next_run_time}) should match the item's new end_time ({rescheduled.end_time})"
    )
    assert rescheduled.checkin_waiting is False, "checkin_waiting should be cleared once answered"
    # nudge_job_id may legitimately be None here: if the new slot landed at
    # the earliest possible moment (nothing else on this user's calendar),
    # a nudge scheduled *before* that start is already in the past by the
    # time we compute it - not a bug, just physically impossible to honor.
    if rescheduled.nudge_job_id is None:
        print("    (nudge is None: new slot is effectively 'now', so 'before it starts' has already passed - expected)")

    print("\nConfirming the old (fired, one-off) nudge job is actually gone, not just abandoned:")
    remaining_ids = {r[0] for r in db.execute(text("SELECT id FROM apscheduler_jobs")).fetchall()}
    print(f"  old nudge job present: {old_nudge_job_id in remaining_ids} (should be False - fired and self-removed)")
    # Not meaningful to check the checkin job id the same way: it's
    # deterministic (f"checkin-{item_id}") and reused via replace_existing,
    # so "checkin-111 present" is expected regardless - see the
    # next_run_time assertion above for the real proof it was rescheduled.

    # restore defaults
    sparse = db.get(User, sparse_uid)
    sparse.nudge_lead_minutes = 30
    no_morning = db.get(User, nm_uid)
    no_morning.nudge_lead_minutes = 30
    db.commit()
    db.close()


def checkin_done_path():
    print("\n" + "=" * 70)
    print("SCENARIO E: check-in answered 'yes, done' - just acknowledges, no reschedule")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, PACKED)
    target = date(2026, 8, 27)

    parsed = parse_message("this is important - review budget, 15 minutes", reference_date=target)
    plan = build_plan(db, uid, target, parsed)
    from app.services.orchestrator import resolve_duration_question

    if plan.pending_duration_questions:
        resolve_duration_question(db, plan, plan.pending_duration_questions[0], 15)
    saved = confirm_plan(db, plan)[0]
    print(f"confirmed id={saved.id} important={saved.important} checkin_job_id={saved.checkin_job_id}")

    # Simulate the check-in job having fired (rather than waiting hours for
    # this far-future item's real end_time) by calling the fire callback directly.
    scheduler_jobs._fire_checkin(saved.id)

    db2 = SessionLocal()
    fired = db2.get(Item, saved.id)
    print(f"checkin_waiting after firing: {fired.checkin_waiting}")

    result = scheduler_jobs.answer_checkin(db2, saved.id, done=True)
    print(f"answer_checkin(done=True) result: {result} (should be None - no reschedule)")

    db2.close()
    db3 = SessionLocal()
    final = db3.get(Item, saved.id)
    print(f"final status={final.status!r} checkin_waiting={final.checkin_waiting} start_time={final.start_time} (unchanged)")
    assert final.status == "done"
    assert final.checkin_waiting is False
    db3.close()


if __name__ == "__main__":
    cleanup_nudge_test_data()
    job_persistence_multi_user()
    recurring_fixed_event_nudge()
    checkin_done_path()
    live_fire_proof()
