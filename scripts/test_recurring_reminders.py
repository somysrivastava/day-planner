import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402
from app.services.briefing import _reminder_occurs_on, generate_briefing_text  # noqa: E402
from app.services.orchestrator import (  # noqa: E402
    build_plan,
    confirm_plan,
    resolve_month_length_warning,
    resolve_reminder_clarification,
)
from app.services.parser import parse_message  # noqa: E402
from app.services.scheduler import get_effective_schedule  # noqa: E402

SPARSE = "+910000000001"


def user_id(db, phone):
    return db.query(User).filter(User.phone_number == phone).first().id


def cleanup(db, uid):
    db.query(Item).filter(Item.user_id == uid, Item.type == "reminder").delete(synchronize_session=False)
    db.commit()


def main():
    db = SessionLocal()
    uid = user_id(db, SPARSE)
    cleanup(db, uid)

    ref = date(2026, 8, 11)  # Tuesday - matches the date used for parser probes above

    print("=" * 70)
    print("1. Parse + confirm a recurring monthly reminder")
    print("=" * 70)
    parsed = parse_message("rent due on the 5th every month", reference_date=ref)
    plan = build_plan(db, uid, ref, parsed)
    print("plan.reminders:", plan.reminders)
    assert not plan.has_pending_issues
    saved = confirm_plan(db, plan)
    rent = next(r for r in saved if r.type == "reminder")
    print(f"saved id={rent.id} title={rent.title!r} start_time={rent.start_time} recurrence_rule={rent.recurrence_rule!r}")
    assert rent.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=5", rent.recurrence_rule
    assert rent.start_time.date() == date(2026, 9, 5), "should resolve to the *next* 5th (this month's already passed)"

    print("\n" + "=" * 70)
    print("2. Parse + confirm a plain (non-recurring) reminder - regression check")
    print("=" * 70)
    parsed2 = parse_message("remind me to submit tax by the 15th", reference_date=ref)
    plan2 = build_plan(db, uid, ref, parsed2)
    saved2 = confirm_plan(db, plan2)
    tax = next(r for r in saved2 if r.type == "reminder")
    print(f"saved id={tax.id} title={tax.title!r} start_time={tax.start_time} recurrence_rule={tax.recurrence_rule!r}")
    assert tax.recurrence_rule is None
    assert tax.start_time.date() == date(2026, 8, 15)

    print("\n" + "=" * 70)
    print("3. _reminder_occurs_on - the recurring reminder across several months/dates")
    print("=" * 70)
    tz = ZoneInfo(db.get(User, uid).timezone)
    checks = [
        (date(2026, 9, 5), True, "the anchor month's 5th"),
        (date(2026, 10, 5), True, "next month's 5th"),
        (date(2027, 1, 5), True, "several months out, still the 5th"),
        (date(2026, 9, 6), False, "day after the 5th"),
        (date(2026, 9, 4), False, "day before the 5th"),
        (date(2026, 8, 5), False, "before the anchor date - RRULE shouldn't recur backward"),
    ]
    for d, expected, label in checks:
        result = _reminder_occurs_on(rent, d, tz)
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] {d} ({label}): expected={expected} got={result}")
        assert result == expected, f"{d} ({label}): expected {expected}, got {result}"

    print("\n" + "=" * 70)
    print("4. The plain reminder only occurs on its exact date, nowhere else")
    print("=" * 70)
    for d, expected in [(date(2026, 8, 15), True), (date(2026, 9, 15), False), (date(2026, 8, 16), False)]:
        result = _reminder_occurs_on(tax, d, tz)
        print(f"  {d}: expected={expected} got={result}")
        assert result == expected

    print("\n" + "=" * 70)
    print("5. Recurring reminder never interacts with the auto-fit scheduler")
    print("=" * 70)
    blocks = get_effective_schedule(db, uid, date(2026, 9, 5))
    titles = [b.title for b in blocks]
    print(f"  effective schedule blocks on 2026-09-05: {titles}")
    assert "rent due" not in titles, "reminders must never appear as scheduler busy-blocks"

    print("\n" + "=" * 70)
    print("6. End-to-end: briefing text on the reminder's next occurrence actually mentions it")
    print("=" * 70)
    text = generate_briefing_text(db, uid, date(2026, 9, 5))
    print(f"\n{text}\n")
    assert "rent" in text.lower(), "briefing text should mention the recurring reminder due that day"

    print("=" * 70)
    print("7. End-to-end: briefing text on a NON-occurrence day does not mention it")
    print("=" * 70)
    text2 = generate_briefing_text(db, uid, date(2026, 9, 12))
    print(f"\n{text2}\n")
    assert "rent" not in text2.lower(), "briefing text should NOT mention a reminder that isn't due that day"

    print("\n" + "=" * 70)
    print("8. Month-length warning surfaces for a recurring reminder anchored on the 31st")
    print("=" * 70)
    parsed31 = parse_message("rent due on the 31st every month", reference_date=ref)
    plan31 = build_plan(db, uid, ref, parsed31)
    print("plan31.pending_month_length_warnings:", plan31.pending_month_length_warnings)
    assert plan31.has_pending_issues
    assert len(plan31.pending_month_length_warnings) == 1
    assert not plan31.reminders, "should NOT be in plan.reminders until the warning is resolved"
    print(plan31.summary_text())
    warning31 = plan31.pending_month_length_warnings[0]

    print("\n" + "=" * 70)
    print("9. 'keep_as_is' path - confirms exactly as parsed, fires only in 31-day months")
    print("=" * 70)
    resolve_month_length_warning(plan31, warning31, "keep_as_is")
    assert not plan31.has_pending_issues
    saved31 = confirm_plan(db, plan31)
    rent31 = next(r for r in saved31 if r.type == "reminder")
    print(f"saved id={rent31.id} recurrence_rule={rent31.recurrence_rule!r} start_time={rent31.start_time}")
    assert rent31.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=31"
    for d, expected, label in [
        (date(2027, 1, 31), True, "January (31 days) - fires"),
        (date(2027, 2, 28), False, "February - no 31st, correctly skipped"),
        (date(2027, 3, 31), True, "March (31 days) - fires"),
        (date(2027, 4, 30), False, "April (30 days) - no 31st, correctly skipped"),
    ]:
        result = _reminder_occurs_on(rent31, d, tz)
        print(f"  [{'OK' if result == expected else 'FAIL'}] {d} ({label}): expected={expected} got={result}")
        assert result == expected

    print("\n" + "=" * 70)
    print("10. 'different_day' path - moves to a fixed day, including rolling forward past a short month")
    print("=" * 70)
    parsed_dd = parse_message("rent due on the 30th every month", reference_date=ref)
    plan_dd = build_plan(db, uid, ref, parsed_dd)
    assert len(plan_dd.pending_month_length_warnings) == 1
    warning_dd = plan_dd.pending_month_length_warnings[0]
    resolve_month_length_warning(plan_dd, warning_dd, "different_day", new_day=15)
    assert not plan_dd.has_pending_issues
    saved_dd = confirm_plan(db, plan_dd)
    rent_dd = next(r for r in saved_dd if r.type == "reminder")
    print(f"saved id={rent_dd.id} recurrence_rule={rent_dd.recurrence_rule!r} start_time={rent_dd.start_time}")
    assert rent_dd.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=15"
    for d in [date(2027, 1, 15), date(2027, 2, 15), date(2027, 4, 15)]:
        assert _reminder_occurs_on(rent_dd, d, tz), f"should fire on the 15th of every month, including {d}"
    print("  fires correctly on the 15th across Jan/Feb/Apr")

    print("\n" + "=" * 70)
    print("11. 'last_day_of_month' adjust path (chosen after being warned) - BYMONTHDAY=-1")
    print("=" * 70)
    parsed_ldm = parse_message("rent due on the 30th every month", reference_date=ref)
    plan_ldm = build_plan(db, uid, ref, parsed_ldm)
    warning_ldm = plan_ldm.pending_month_length_warnings[0]
    resolve_month_length_warning(plan_ldm, warning_ldm, "last_day_of_month")
    assert not plan_ldm.has_pending_issues
    saved_ldm = confirm_plan(db, plan_ldm)
    rent_ldm = next(r for r in saved_ldm if r.type == "reminder")
    print(f"saved id={rent_ldm.id} recurrence_rule={rent_ldm.recurrence_rule!r} start_time={rent_ldm.start_time}")
    assert rent_ldm.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=-1"
    for d, label in [(date(2027, 1, 31), "31-day month"), (date(2027, 2, 28), "28-day month"), (date(2027, 4, 30), "30-day month")]:
        assert _reminder_occurs_on(rent_ldm, d, tz), f"should fire on the actual last day ({label}): {d}"
    print("  fires correctly on the actual last day across 3 different-length months")

    print("\n" + "=" * 70)
    print("12. Direct 'last day of every month' phrasing (no warning - already the safe option)")
    print("=" * 70)
    parsed_direct = parse_message("remind me to water the plants on the last day of every month", reference_date=ref)
    print("parsed:", parsed_direct.items)
    plan_direct = build_plan(db, uid, ref, parsed_direct)
    assert not plan_direct.pending_month_length_warnings, "last_day_of_month should never trigger the warning"
    assert not plan_direct.has_pending_issues
    saved_direct = confirm_plan(db, plan_direct)
    plants = next(r for r in saved_direct if r.type == "reminder")
    print(f"saved id={plants.id} recurrence_rule={plants.recurrence_rule!r} start_time={plants.start_time}")
    assert plants.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=-1"

    print("\n" + "=" * 70)
    print("13. Advance notice on a recurring reminder - independent recurring reminders, not dropped")
    print("=" * 70)
    parsed_adv = parse_message("remind me 3 days before rent is due every month, on the 5th", reference_date=ref)
    print("parsed:", parsed_adv.items)
    assert len(parsed_adv.items) == 2, f"expected 2 independently-recurring items, got {len(parsed_adv.items)}"
    assert all(item.recurring for item in parsed_adv.items), "both must be recurring, not a one-time fan-out"
    plan_adv = build_plan(db, uid, ref, parsed_adv)
    assert not plan_adv.has_pending_issues
    saved_adv = confirm_plan(db, plan_adv)
    adv_reminders = [r for r in saved_adv if r.type == "reminder"]
    print("saved:", [(r.id, r.title, r.start_time.date(), r.recurrence_rule) for r in adv_reminders])
    assert len(adv_reminders) == 2
    rrules = {r.recurrence_rule for r in adv_reminders}
    assert rrules == {"FREQ=MONTHLY;BYMONTHDAY=5", "FREQ=MONTHLY;BYMONTHDAY=2"}, rrules
    # Each fires independently and correctly across several months - not
    # just once, and not coupled to each other.
    for r in adv_reminders:
        expected_day = 5 if r.recurrence_rule.endswith("=5") else 2
        for month_date in [date(2026, 9, expected_day), date(2026, 10, expected_day), date(2026, 11, expected_day)]:
            assert _reminder_occurs_on(r, month_date, tz), f"id={r.id} should fire on {month_date}"
        # And does NOT fire on the *other* reminder's day - proves these
        # are two independent RRULEs, not one rule misapplied twice.
        other_day = 2 if expected_day == 5 else 5
        assert not _reminder_occurs_on(r, date(2026, 9, other_day), tz)
    print("  both fire independently and correctly across 3 months, and don't bleed into each other's day")

    print("\n" + "=" * 70)
    print("14. Nonsensical day-of-month ('the 0th') is flagged, never silently guessed")
    print("=" * 70)
    parsed_zero = parse_message("rent due on the 0th every month", reference_date=ref)
    plan_zero = build_plan(db, uid, ref, parsed_zero)
    print("plan_zero.pending_reminder_clarifications:", plan_zero.pending_reminder_clarifications)
    assert plan_zero.has_pending_issues
    assert len(plan_zero.pending_reminder_clarifications) == 1
    assert not plan_zero.reminders, "must not be silently confirmed with a guessed date"
    print(plan_zero.summary_text())

    print("\n" + "=" * 70)
    print("15. Resolving the clarification with a plain corrected date")
    print("=" * 70)
    item_zero = plan_zero.pending_reminder_clarifications[0]
    resolve_reminder_clarification(plan_zero, item_zero, "2026-09-05")
    assert not plan_zero.has_pending_issues
    saved_zero = confirm_plan(db, plan_zero)
    rent_zero = next(r for r in saved_zero if r.type == "reminder")
    print(f"saved id={rent_zero.id} recurrence_rule={rent_zero.recurrence_rule!r} start_time={rent_zero.start_time}")
    assert rent_zero.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=5"

    print("\n" + "=" * 70)
    print("16. Resolving the clarification with 'last day of the month' instead")
    print("=" * 70)
    parsed_zero2 = parse_message("rent due on the 0th every month", reference_date=ref)
    plan_zero2 = build_plan(db, uid, ref, parsed_zero2)
    item_zero2 = plan_zero2.pending_reminder_clarifications[0]
    resolve_reminder_clarification(plan_zero2, item_zero2, "2026-08-18", last_day_of_month=True)
    assert not plan_zero2.has_pending_issues
    saved_zero2 = confirm_plan(db, plan_zero2)
    rent_zero2 = next(r for r in saved_zero2 if r.type == "reminder")
    print(f"saved id={rent_zero2.id} recurrence_rule={rent_zero2.recurrence_rule!r} start_time={rent_zero2.start_time}")
    assert rent_zero2.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=-1"

    print("\n" + "=" * 70)
    print("17. Resolving into a 29th/30th/31st correctly chains into the month-length warning, not straight to confirm")
    print("=" * 70)
    parsed_zero3 = parse_message("rent due on the 0th every month", reference_date=ref)
    plan_zero3 = build_plan(db, uid, ref, parsed_zero3)
    item_zero3 = plan_zero3.pending_reminder_clarifications[0]
    resolve_reminder_clarification(plan_zero3, item_zero3, "2026-08-31")
    print("pending_month_length_warnings:", [(w.reminder.title, w.reminder.date) for w in plan_zero3.pending_month_length_warnings])
    assert plan_zero3.has_pending_issues, "should chain into the 29-31 warning, not confirm directly"
    assert len(plan_zero3.pending_month_length_warnings) == 1
    assert not plan_zero3.reminders
    print("  correctly chained into pending_month_length_warnings instead of silently confirming")

    db.close()
    print("\nALL RECURRING REMINDER CHECKS PASSED")


if __name__ == "__main__":
    main()
