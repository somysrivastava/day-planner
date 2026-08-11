"""Week 2 Day 4 (extended): adversarial/fuzz-style parser testing beyond
the existing ~12-13 scripted phrases in test_parser.py. Goal is specifically
to hunt for misclassification -> downstream-crash patterns like the one
found today (a bare time-of-day word getting misclassified as a recurring
task with a malformed time field that crashed _promote_recurring_task
uncaught). Every phrase below is run through parse_message -> build_plan
(the real pipeline, not a mock), and any exception is reported - not just
the ones matching today's specific shape.
"""

import sys
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402
from app.services.orchestrator import build_plan, confirm_plan  # noqa: E402
from app.services.parser import (  # noqa: E402
    ExplicitTimeItem,
    ParsedMessage,
    RecurringTaskItem,
    RecurringTime,
    TaskItem,
    parse_message,
)

SPARSE = "+910000000001"

REFERENCE_DATE = date(2026, 8, 18)  # Tuesday - matches an existing clean-schedule test date

PHRASES = [
    # Bare time-of-day words, various positions/phrasings - the exact class
    # of today's bug, probed more broadly than just the one failing phrase.
    "gym workout, 1 hour, morning",
    "call the dentist, 20 minutes, afternoon",
    "read, 45 minutes, night",
    "morning workout, 30 minutes",
    "afternoon nap, 1 hour",
    "quick errand this evening, 15 minutes",
    "do it in the morning sometime, 20 minutes",
    # Weird/unusual durations.
    "meditate for a bit, 5 minutes",
    "deep work session, 4 hours",
    "quick sync, 90 seconds",
    "review the doc, half an hour",
    "call mom, an hour and a half",
    "pack for the trip, 0 minutes",
    "water the plants, -10 minutes",
    # Ambiguous / vague dates.
    "pay the electricity bill sometime next week",
    "renew passport eventually",
    "call the plumber the day after tomorrow, 30 minutes",
    "submit report next next Monday, 1 hour",
    "birthday party on the 32nd",
    "rent due on the 0th every month",
    "quarterly review on February 30th",
    # Recurring edge cases beyond MO-SU BYDAY.
    "gym every weekday, 1 hour",
    "call mom every weekend, 20 minutes",
    "stretch every other day, 10 minutes",
    "team sync every single day at 9am, 15 minutes",
    "board game night monthly, 2 hours",
    # Explicit time weirdness.
    "meeting at 25:00, 30 minutes",
    "call at noon, 15 minutes",
    "standup at 9am sharp, 15 minutes",
    "lunch from 12 to 1",
    "call at 9:99pm, 10 minutes",
    # Multi-item messages mixing clean and messy items.
    "gym in the morning, 1 hour, and also call mom, evening, 20 minutes",
    "rent due on the 31st every month, and also water plants, last day of every month",
    "prep slides, 20 minutes, urgent, and this is important - call the bank at 3pm",
    # Deliberately weird/garbage/edge-of-domain input.
    "asdkfjaslkdfj",
    "",
    "??? what do I even have today",
    "lol nothing today just vibing",
    "remind remind remind me to remind myself",
    "🎉🎉🎉 party time, 30 minutes 🎉🎉🎉",
    "TODO: figure out life, 1000000 minutes",
    # Long rambling message.
    (
        "okay so tomorrow is gonna be a lot, first I need to hit the gym in the morning for "
        "like an hour, then at some point call the insurance company about the claim which is "
        "important, then in the afternoon maybe squeeze in a nap, and also don't forget rent "
        "is due on the 31st every month, and oh also team standup every weekday at 9am for 15 "
        "minutes, and honestly if I have time read for half an hour in the evening"
    ),
]

# Second batch (added after the user asked for genuinely new coverage, not
# more phrasings of the three bug classes already found): malformed
# durations, garbage recurrence patterns, multi-item messages with one
# deliberately malformed item mixed with valid ones, and empty/whitespace/
# extremely long input.
MORE_PHRASES = [
    # Malformed/extreme durations - duration_minutes is a schema-typed
    # Optional[int], so the model can't literally return "banana", but it
    # can return nonsensical *integers*, or get confused into leaving it
    # null when the input is itself absurd.
    "gym workout, 99999999 minutes",
    "call mom, negative thirty minutes",
    "meeting, duration: yes, 3pm",
    "task that takes forever, 1 hour",
    "quick thing, 0.5 minutes",
    "call the bank, three point five hours",
    "errand, a gazillion minutes",
    "standup at 9am, duration unknown",
    # Garbage / self-contradictory recurrence patterns - days is a
    # schema-constrained enum list so it can't be garbage text, but the
    # *combination* of days/times/duration can still be nonsensical.
    "recurring task with no days specified, 20 minutes",
    "gym every day except never, 1 hour",
    "team sync every weekday and also never, 15 minutes",
    "stretch every day of the week twice a day, 10 minutes",
    "read Monday through Sunday but only on weekdays, 30 minutes",
    "workout on Smday, 1 hour",  # nonsense weekday name
    "meeting every 3rd Tuesday of the month, 30 minutes",  # unsupported recurrence shape entirely
    # Multi-item messages with one deliberately malformed item mixed among valid ones -
    # the real question is whether one bad item takes down the *whole* message
    # or whether the good items still get through.
    "gym at 3pm today, 1 hour, and rent due on the 45th every month",
    "call mom, 20 minutes, and meeting at 99:99, 30 minutes, and read, 15 minutes",
    "submit report on February 30th, and also gym at 6am, 1 hour",
    "team sync every day at 25:00, 15 minutes, and lunch at noon, 30 minutes",
    "rent due on the 0th every month, and also pick up dry cleaning, 20 minutes",
    # Empty / whitespace-only / extreme-length input.
    "   ",
    "\n\n\t\n",
    ".",
    "a" * 3000,
    " ".join(["gym", "call mom", "read", "meeting", "errand"] * 100),
]

# Third and final batch (bounded, targeted final check per the user's
# explicit request) - categories genuinely untried by the first two
# batches: multiple conflicting recurrence patterns in one message,
# extremely vague natural language with no resolvable time information at
# all, non-English/mixed-script input, and SQL-injection-style strings
# specifically to confirm the DB layer (SQLAlchemy ORM, parameterized
# queries throughout) isn't naively trusting raw text.
FINAL_PHRASES = [
    # Conflicting/self-contradictory recurrence patterns in one message.
    "gym every Monday and also every day, 1 hour",
    "meeting every week on Tuesday but also just once next Friday, 30 minutes",
    "read daily but skip weekends and also just on Mondays, 20 minutes",
    "call mom every day and never again, 15 minutes",
    "rent due on the 5th every month and also just this one time on the 10th",
    # Extremely ambiguous natural language - no resolvable time information at all.
    "call the dentist sometime",
    "do the thing whenever, 20 minutes",
    "gym at some point today, 1 hour",
    "finish the project eventually",
    "handle it, 15 minutes",
    # Non-English / mixed scripts.
    "जिम जाना है, 1 घंटा",
    "健身房锻炼，1小时",
    "call مامي at 3pm, 20 minutes",
    "встреча завтра в 3, 30 минут",
    "café ☕ break, 15 minutes",
    # SQL-injection-style strings - the DB layer should treat these as
    # inert title text (SQLAlchemy ORM, parameterized inserts), never as
    # executable SQL.
    "'; DROP TABLE items; --, 20 minutes",
    "task titled Robert'); DROP TABLE users;--, 30 minutes",
    "meeting at 3pm called ' OR '1'='1",
    'remind me about " UNION SELECT * FROM users -- on the 5th',
    "gym'); DELETE FROM items WHERE '1'='1, 1 hour",
]


def run_case(db, uid, text):
    try:
        parsed = parse_message(text, reference_date=REFERENCE_DATE)
    except Exception as exc:
        return ("PARSE_CRASH", exc, None)

    try:
        plan = build_plan(db, uid, REFERENCE_DATE, parsed)
    except Exception as exc:
        return ("BUILD_PLAN_CRASH", exc, parsed)

    return ("OK", None, parsed)


def run_compatibility_check(db, uid):
    """Deterministic (no LLM involved) check that this morning's
    _promote_recurring_task/_parse_hhmm fix and this afternoon's
    _safe_parse_date/entry-point-guard fix are actually compatible - built
    directly rather than hoping the LLM produces both malformed shapes in
    one message reliably enough to trust. One ParsedMessage with THREE
    items: a recurring task with a malformed time (this morning's bug
    class), an explicit-time item with a malformed date (this afternoon's
    bug class, on the date side), and one perfectly valid task - confirms
    both fixes fire independently in the same build_plan call, neither
    swallows or interferes with the other, and the valid item still makes
    it through untouched."""
    print("\n" + "=" * 70)
    print("COMPATIBILITY CHECK: both fixes exercised together in one build_plan call")
    print("=" * 70)

    bad_recurring = RecurringTaskItem(
        type="recurring_task", title="gym workout", days=["MO", "TU"], duration_minutes=60,
        times=[RecurringTime(day="MO", time="morning"), RecurringTime(day="TU", time="09:00")],
    )
    bad_explicit = ExplicitTimeItem(
        type="explicit_time_item", title="quarterly review", date="2026-02-30",
        start_time="14:00", duration_minutes=30,
    )
    good_task = TaskItem(type="task", title="read a book", day="2026-08-18", duration_minutes=20)

    parsed = ParsedMessage(items=[bad_recurring, bad_explicit, good_task])
    plan = build_plan(db, uid, REFERENCE_DATE, parsed)  # must not raise

    # The malformed recurring time (MO) should have been routed to
    # auto-fit rather than crashing, while the valid one (TU, 09:00)
    # should still have produced a real fixed-time group.
    recurring_days_covered = {d for g in plan.recurring_groups for d in g.days}
    print(f"  recurring_groups days covered: {sorted(recurring_days_covered)}")
    assert "TU" in recurring_days_covered, "the well-formed Tuesday time should still have promoted cleanly"

    # The malformed explicit-time item (invalid Feb 30 date) should have
    # been dropped entirely, not crashed the plan and not silently placed.
    explicit_titles = {p.title for p in plan.explicit_placements}
    print(f"  explicit_placements titles: {explicit_titles}")
    assert "quarterly review" not in explicit_titles, "the Feb 30 item should have been dropped, not placed"

    # The valid, unrelated task should be completely unaffected by either
    # of the other two items being malformed.
    task_titles = {p.title for p in plan.task_placements}
    print(f"  task_placements titles: {task_titles}")
    assert "read a book" in task_titles, "the valid task must not be collateral damage from the other two bad items"

    print("  COMPATIBILITY CHECK PASSED - both fixes fire independently, no gap, no interference")
    return True


def run_sql_injection_db_check(db, uid):
    """Confirms the DB layer treats a SQL-injection-style title as inert
    text, not executable SQL - goes further than 'did build_plan crash' by
    actually writing it through confirm_plan (a real INSERT via SQLAlchemy
    ORM) and reading it back, plus checking table row counts before/after
    to prove nothing was dropped/deleted. Constructed directly (not via the
    LLM) so the exact malicious string is guaranteed, not just probable."""
    print("\n" + "=" * 70)
    print("SQL-INJECTION DB-LAYER CHECK: a malicious title survives a real INSERT + SELECT unchanged")
    print("=" * 70)

    malicious_title = "'; DROP TABLE items; -- Robert'); DELETE FROM users WHERE '1'='1"

    users_before = db.query(User).count()
    items_before = db.query(Item).count()

    item = TaskItem(type="task", title=malicious_title, day="2026-08-18", duration_minutes=15)
    parsed = ParsedMessage(items=[item])
    plan = build_plan(db, uid, REFERENCE_DATE, parsed)
    assert not plan.has_pending_issues, "a plain title/day/duration task shouldn't hit any pending question"
    saved = confirm_plan(db, plan)
    assert len(saved) == 1

    # Fresh query, not just the in-memory object, to actually prove a real
    # round trip through the DB - not trusting the ORM object we still
    # happen to be holding a reference to.
    db.expire_all()
    row = db.get(Item, saved[0].id)
    print(f"  stored title: {row.title!r}")
    assert row.title == malicious_title, "the title must survive byte-for-byte, proving it was bound as data, not executed"

    users_after = db.query(User).count()
    items_after = db.query(Item).count()
    print(f"  users: {users_before} -> {users_after} (must be unchanged)")
    print(f"  items: {items_before} -> {items_after} (must be exactly +1)")
    assert users_after == users_before, "the 'DELETE FROM users' fragment must not have executed"
    assert items_after == items_before + 1, "the 'DROP TABLE items' fragment must not have executed"

    db.delete(row)
    db.commit()
    print("  SQL-INJECTION DB-LAYER CHECK PASSED - stored as inert text, tables intact, row counts correct")
    return True


def main():
    db = SessionLocal()
    uid = db.query(User).filter(User.phone_number == SPARSE).first().id

    all_phrases = PHRASES + MORE_PHRASES + FINAL_PHRASES
    results = {"OK": 0, "PARSE_CRASH": 0, "BUILD_PLAN_CRASH": 0}
    failures = []

    for i, text in enumerate(all_phrases):
        status, exc, parsed = run_case(db, uid, text)
        results[status] += 1
        marker = "OK  " if status == "OK" else "FAIL"
        label = text if len(text) <= 70 else f"{text[:67]}..."
        print(f"[{marker}] ({i + 1}/{len(all_phrases)}) {status:18s} {label!r}")
        if status != "OK":
            failures.append((text, status, exc))
            print(f"       {type(exc).__name__}: {exc}")

    compat_ok = True
    try:
        run_compatibility_check(db, uid)
    except Exception as exc:
        compat_ok = False
        print(f"  COMPATIBILITY CHECK FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    sql_ok = True
    try:
        run_sql_injection_db_check(db, uid)
    except Exception as exc:
        sql_ok = False
        print(f"  SQL-INJECTION DB-LAYER CHECK FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    db.close()

    print("\n" + "=" * 70)
    print(
        f"Fuzz results: {results['OK']}/{len(all_phrases)} OK, "
        f"{results['PARSE_CRASH']} parse crashes, {results['BUILD_PLAN_CRASH']} build_plan crashes"
    )
    print(f"Compatibility check: {'PASSED' if compat_ok else 'FAILED'}")
    print(f"SQL-injection DB-layer check: {'PASSED' if sql_ok else 'FAILED'}")
    print("=" * 70)

    if failures:
        print("\nFull tracebacks for failures:\n")
        for text, status, exc in failures:
            print(f"--- {status}: {text!r} ---")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            print()

    return len(failures) + (0 if compat_ok else 1) + (0 if sql_ok else 1)


if __name__ == "__main__":
    n_failures = main()
    sys.exit(1 if n_failures else 0)
