import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services.orchestrator import (  # noqa: E402
    build_plan,
    confirm_plan,
    remove_item,
    reschedule_item,
    resolve_clarification,
    resolve_collision,
    resolve_duration_question,
    resolve_no_fit,
    route_message,
    skip_fixed_event,
)
from app.db.models import FixedEventOverride, Item  # noqa: E402
from app.services.parser import parse_message  # noqa: E402
from app.services.scheduler import get_effective_schedule  # noqa: E402

YOU = "+919354211791"
SPARSE = "+910000000001"
PACKED = "+910000000002"
NO_MORNING = "+910000000003"

SEED_ANCHOR_DATES = {date(2026, 8, 10), date(2026, 8, 11)}  # MONDAY/TUESDAY from seed_fixed_events.py


def user_id(db, phone):
    return db.query(User).filter(User.phone_number == phone).first().id


def cleanup_test_data(db):
    """This script isn't idempotent like seed_fixed_events.py - re-running
    it accumulates tasks/promoted-recurring-events/overrides from previous
    runs, which drifts placement times and creates duplicate-looking
    entries. Reset to a clean baseline before each run: delete all tasks
    (never seeded, always test-created), all overrides, and any
    fixed_event whose anchor date isn't one of the two seed anchors
    (i.e. a promoted recurring task from a previous test run)."""
    uids = [user_id(db, p) for p in (YOU, SPARSE, PACKED, NO_MORNING)]
    db.query(Item).filter(Item.user_id.in_(uids), Item.type == "task").delete(synchronize_session=False)
    db.query(FixedEventOverride).filter(
        FixedEventOverride.item_id.in_(db.query(Item.id).filter(Item.user_id.in_(uids)))
    ).delete(synchronize_session=False)
    for item in db.query(Item).filter(Item.user_id.in_(uids), Item.type == "fixed_event").all():
        if item.start_time.date() not in SEED_ANCHOR_DATES:
            db.delete(item)
    db.commit()


def answer_duration(db, plan, duration_minutes, target):
    """Answers whichever pending-info state a task landed in with a plain
    duration (both paths converge on the same question in practice, since
    day is usually already resolved by the parser - see parser.py's
    documented needs_clarification known-limitation comment)."""
    while plan.pending_duration_questions:
        task = plan.pending_duration_questions[0]
        print(f"Bot asks: \"how long do you think '{task.title}' will take?\" -> you say {duration_minutes} min")
        resolve_duration_question(db, plan, task, duration_minutes)
    while plan.pending_clarifications:
        task = plan.pending_clarifications[0]
        print(f"Bot asks: \"how long do you think '{task.title}' will take?\" (day already known) -> you say {duration_minutes} min")
        resolve_clarification(db, plan, task, day=target, duration_minutes=duration_minutes)


def show_schedule(db, uid, d, label):
    print(f"{label} ({d}, {d.strftime('%A')}):")
    blocks = get_effective_schedule(db, uid, d)
    if not blocks:
        print("  (nothing)")
    for b in blocks:
        print(f"  {b.start.strftime('%H:%M')}-{b.end.strftime('%H:%M')}  {b.title}  [{b.source}]")
    print()


def worked_example():
    print("=" * 70)
    print("SCENARIO 1: Worked example from docs/scheduler-algorithm.md")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, YOU)
    target = date(2026, 8, 18)  # Tuesday

    show_schedule(db, uid, target, "Morning briefing baseline")

    print("You reply: 'skip gym today, and I need to pick up my dress, and read for 30 minutes daily starting today.'\n")
    skip_fixed_event(db, uid, target, "Gym")

    parsed = parse_message(
        "I need to pick up my dress, and read for 30 minutes daily starting today.", reference_date=target
    )
    print("Parsed items:")
    for item in parsed.items:
        print(f"  {item}")
    print()

    plan = build_plan(db, uid, target, parsed)
    print("Initial plan:")
    print(plan.summary_text())
    print()

    answer_duration(db, plan, 30, target)
    print()

    print("Final plan:")
    print(plan.summary_text())
    print()

    print("You say yes -> confirming (writes to items table + syncs to Google Calendar):")
    saved = confirm_plan(db, plan)
    for row in saved:
        print(f"  saved id={row.id} type={row.type} title={row.title!r} start={row.start_time} google_event_id={row.google_event_id}")

    print("\nEffective schedule for the day AFTER confirmation:")
    show_schedule(db, uid, target, "Post-confirmation")

    print("Effective schedule for tomorrow (gym should be back - skip was temporary):")
    show_schedule(db, uid, date(2026, 8, 19), "Wednesday")

    db.close()


def collision_with_task():
    print("=" * 70)
    print("SCENARIO 2: explicit-time item collides with an existing TASK")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, YOU)
    target = date(2026, 8, 20)  # Thursday

    # Seed an existing task first (simulating something placed earlier today).
    parsed1 = parse_message("gym prep, 30 minutes", reference_date=target)
    plan1 = build_plan(db, uid, target, parsed1)
    answer_duration(db, plan1, 30, target)
    print("Pre-existing task placed:")
    print(plan1.summary_text())
    confirm_plan(db, plan1)
    print()

    show_schedule(db, uid, target, "Effective schedule before the collision")

    # Now an explicit-time item that collides with wherever that task landed.
    existing_start = plan1.task_placements[0].start
    hh, mm = existing_start.hour, existing_start.minute
    parsed2 = parse_message(f"bank manager meeting at {hh}:{mm:02d}", reference_date=target)
    plan2 = build_plan(db, uid, target, parsed2)
    print("New plan (should show a pending collision):")
    print(plan2.summary_text())
    print()

    if plan2.pending_collisions:
        c = plan2.pending_collisions[0]
        print(f"Bot asks: \"You already have '{c.colliding_task_title}' scheduled then - should I move that one, or keep this at a different time?\"")
        print("You say: move the existing one\n")
        resolve_collision(db, plan2, c, "move_existing")

    print("Final plan after resolving collision:")
    print(plan2.summary_text())
    saved = confirm_plan(db, plan2)
    for row in saved:
        print(f"  saved id={row.id} title={row.title!r} start={row.start_time}")

    db.close()


def no_fit_and_force():
    print("=" * 70)
    print("SCENARIO 3: no-fit case, tightly-packed user")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, PACKED)
    target = date(2026, 8, 19)  # Wednesday

    show_schedule(db, uid, target, "Packed user's day (08:00-19:30 solid)")

    parsed = parse_message("deep focus block, 3 hours, sometime in the afternoon", reference_date=target)
    plan = build_plan(db, uid, target, parsed)
    answer_duration(db, plan, 180, target)
    print("Plan (should show a no-fit):")
    print(plan.summary_text())
    print()

    if plan.pending_no_fits:
        nf = plan.pending_no_fits[0]
        print(f"Bot asks: \"'{nf.item_title}' doesn't fit today - shift to tomorrow, or must it happen today?\"")
        print("You say: it must happen today (force it)\n")
        resolve_no_fit(db, plan, nf, "force_today")

    print("Final plan (forced placement, sleep-hours authorized):")
    print(plan.summary_text())
    print("\nConfirming (this user has no Google Calendar connected - sync should be skipped gracefully, not crash):")
    saved = confirm_plan(db, plan)
    for row in saved:
        print(f"  saved id={row.id} title={row.title!r} start={row.start_time} google_event_id={row.google_event_id}")

    db.close()


def recurring_per_day_times():
    print("=" * 70)
    print("SCENARIO 4: recurring task promotion with per-day-varying times")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, SPARSE)
    target = date(2026, 8, 17)  # Monday

    parsed = parse_message("read Mon/Wed/Fri at 9pm but 6pm on Saturday", reference_date=target)
    print("Parsed items:")
    for item in parsed.items:
        print(f"  {item}")
    print()

    plan = build_plan(db, uid, target, parsed)
    print("Plan:")
    print(plan.summary_text())
    print()

    print("Confirming (promotes to fixed_event rows with RRULEs):")
    saved = confirm_plan(db, plan)
    for row in saved:
        print(f"  saved id={row.id} title={row.title!r} start={row.start_time} rrule={row.recurrence_rule!r}")

    print("\nEffective schedule check - Monday (should show 9pm) vs Saturday (should show 6pm):")
    show_schedule(db, uid, date(2026, 8, 17), "Monday")
    show_schedule(db, uid, date(2026, 8, 22), "Saturday")

    db.close()


def no_morning_user_task_placement():
    print("=" * 70)
    print("SCENARIO 5: NO_MORNING user - untimed task should land early (whole morning open)")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, NO_MORNING)
    target = date(2026, 8, 18)  # Tuesday - this user's only commitment is Evening Class 18:30-20:00

    show_schedule(db, uid, target, "Baseline (only an evening class, Tue/Thu)")

    parsed = parse_message("gym workout, 1 hour, morning", reference_date=target)
    plan = build_plan(db, uid, target, parsed)
    answer_duration(db, plan, 60, target)
    print("Plan (should land early morning - nothing else is scheduled before the evening class):")
    print(plan.summary_text())
    saved = confirm_plan(db, plan)
    for row in saved:
        print(f"  saved id={row.id} title={row.title!r} start={row.start_time}")

    db.close()


def sparse_user_task_placement():
    print("=" * 70)
    print("SCENARIO 6: SPARSE user - untimed task around its one 15-min standup")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, SPARSE)
    target = date(2026, 8, 19)  # Wednesday - only commitment is a 09:00-09:15 standup

    show_schedule(db, uid, target, "Baseline (one 15-min standup, otherwise wide open)")

    parsed = parse_message("write blog post, 2 hours, afternoon", reference_date=target)
    plan = build_plan(db, uid, target, parsed)
    answer_duration(db, plan, 120, target)
    print("Plan (huge open afternoon - should place easily, unlike the packed user's no-fit case):")
    print(plan.summary_text())
    saved = confirm_plan(db, plan)
    for row in saved:
        print(f"  saved id={row.id} title={row.title!r} start={row.start_time}")

    db.close()


def mixed_date_routing():
    print("=" * 70)
    print("SCENARIO 7: route_message() - one message spanning two dates")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, YOU)
    reference = date(2026, 8, 24)  # Monday

    parsed = parse_message(
        "get a haircut today, and submit the tax report by Wednesday, needs an hour", reference_date=reference
    )
    print("Parsed items:")
    for item in parsed.items:
        print(f"  {item}")
    print()

    plans = route_message(db, uid, reference, parsed)
    print(f"route_message produced {len(plans)} plan(s), for dates: {sorted(plans.keys())}\n")

    for d, plan in sorted(plans.items()):
        answer_duration(db, plan, 60, d)
        print(f"--- Plan for {d} ---")
        print(plan.summary_text())
        print(f"out_of_scope_items: {plan.out_of_scope_items} (should be empty - route_message partitions up front)")
        confirm_plan(db, plan)
        print()

    db.close()


def reject_and_change_flow():
    print("=" * 70)
    print("SCENARIO 8: plan rejection - 'no, what would you like changed?'")
    print("=" * 70)
    db = SessionLocal()
    uid = user_id(db, YOU)
    target = date(2026, 8, 21)  # Friday

    parsed = parse_message("pay electricity bill, 15 minutes, and call the plumber, 20 minutes", reference_date=target)
    plan = build_plan(db, uid, target, parsed)
    answer_duration(db, plan, 15, target)  # in case either needed it (both stated durations, likely unused)

    print("Initial plan:")
    print(plan.summary_text())
    print("\nYou say: no")
    print("Bot asks: \"okay, what would you like changed?\"")

    bill_title = plan.task_placements[0].title
    plumber_title = plan.task_placements[1].title
    print(f"You say: drop '{bill_title}', and move '{plumber_title}' to the evening\n")

    removed = remove_item(plan, bill_title)
    print(f"  removed '{bill_title}': {removed}")
    no_fit = reschedule_item(db, plan, plumber_title, new_time_of_day_preference="evening")
    print(f"  rescheduled '{plumber_title}' to evening, no_fit={no_fit}")

    print("\nUpdated plan (not restarted from scratch, just adjusted):")
    print(plan.summary_text())

    print("\nYou say: yes")
    saved = confirm_plan(db, plan)
    for row in saved:
        print(f"  saved id={row.id} title={row.title!r} start={row.start_time}")
    print(f"  ('{bill_title}' correctly absent from saved rows - it was dropped, not just hidden)")

    db.close()


if __name__ == "__main__":
    cleanup_test_data(SessionLocal())
    worked_example()
    print()
    collision_with_task()
    print()
    no_fit_and_force()
    print()
    recurring_per_day_times()
    print()
    no_morning_user_task_placement()
    print()
    sparse_user_task_placement()
    print()
    mixed_date_routing()
    print()
    reject_and_change_flow()
