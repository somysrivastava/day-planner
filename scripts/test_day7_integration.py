"""Day 7, Part 3: drives the real scripts/cli.py functions (not a parallel
reimplementation) through one continuous simulated day for the real user -
morning briefing, adding tasks by text, a pre-task nudge, an important task's
completion check-in (answered 'no' -> reschedule), and the evening check-in
sweeping up everything still left over (answered all three ways: tomorrow,
choose a date, and leaving it pending).

input() is scripted (not a human typing) so this can run unattended and
assert on real DB/job-store state at each step, same rigor as
test_scheduler.py/test_nudges.py/test_voice.py - but every call below goes
through cli.py's actual handle_message/dispatch_command/confirm_now, so this
is genuinely testing the CLI stub, not bypassing it.
"""

import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item  # noqa: E402
from scripts import cli  # noqa: E402
from scripts.test_scheduler import cleanup_test_data  # noqa: E402

YOU = "you"


def cleanup():
    """Same idempotency need as test_scheduler.py/test_nudges.py - re-running
    this without cleanup accumulates tasks and drifts placement times."""
    db = SessionLocal()
    cleanup_test_data(db)
    db.execute(text("DELETE FROM apscheduler_jobs"))
    db.commit()
    db.close()


@contextmanager
def scripted_input(default_duration=None):
    """Routes on prompt content rather than a fixed answer sequence -
    necessary because the parser's known non-determinism (see PROGRESS.md)
    means the same message can occasionally land in a pending_clarification
    or pending_duration_question branch even when the text gave a duration
    outright, so a strict FIFO answer queue would desync depending on which
    branch actually fires on any given run."""

    def fake_input(prompt=""):
        p = prompt.lower()
        if "how long" in p:
            val = str(default_duration)
        elif "when should" in p:
            val = "today"
        elif "time of day preference" in p:
            val = ""
        elif "confirm this plan" in p:
            val = "yes"
        elif "collides with existing" in p:
            val = "keep"
        elif "no fit for" in p:
            val = "tomorrow"
        else:
            raise AssertionError(f"No scripted answer for prompt: {prompt!r}")
        print(f"{prompt}{val}")
        return val

    with patch("builtins.input", fake_input):
        yield


def latest_item_by_title(db, user_id, needle):
    return (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.title.ilike(f"%{needle}%"))
        .order_by(Item.id.desc())
        .first()
    )


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main():
    cleanup()
    db = SessionLocal()
    user = cli.get_user(db, YOU)
    sess = cli.Session(db, user)
    today = date.today()
    print(f"User: {user.phone_number} (id={user.id}), simulated 'today' = {today}")

    section(":briefing - morning briefing fires")
    cli.dispatch_command(sess, ":briefing")

    section("Add a task by text: 'prep slides, 20 minutes'")
    with scripted_input(default_duration=20):
        cli.handle_message(sess, "prep slides, 20 minutes")

    section("Add an explicit-time item: 'quick call at 7:30pm, 10 minutes'")
    with scripted_input(default_duration=10):
        cli.handle_message(sess, "quick call at 7:30pm, 10 minutes")

    section("Add an explicit-time item: 'team sync at 8pm, 15 minutes'")
    with scripted_input(default_duration=15):
        cli.handle_message(sess, "team sync at 8pm, 15 minutes")

    section("Add an important task: 'finish the client proposal, this is important, 30 minutes'")
    with scripted_input(default_duration=30):
        cli.handle_message(sess, "finish the client proposal, this is important, 30 minutes")

    cli.dispatch_command(sess, ":items")

    slides = latest_item_by_title(db, user.id, "prep slides")
    quickcall = latest_item_by_title(db, user.id, "quick call")
    teamsync = latest_item_by_title(db, user.id, "team sync")
    proposal = latest_item_by_title(db, user.id, "client proposal")
    assert slides and quickcall and teamsync and proposal, "one of the 4 test items didn't get created"
    assert proposal.important is True

    section(f"Pre-task nudge fires manually for 'prep slides' (id={slides.id})")
    cli.dispatch_command(sess, f":nudge {slides.id}")

    section(f"Backdating 'prep slides' too, so it's also genuinely eligible for tonight's evening sweep (id={slides.id})")
    # Same reasoning as the proposal backdate below: the Day 7 min_start fix
    # (see orchestrator._effective_start_bound) means a task added this
    # evening with no time preference now correctly lands *later this
    # evening*, not in the past - which is the right fix, but it also means
    # this freshly-confirmed task hasn't actually happened yet by the time
    # the evening check-in fires moments later in real time. Backdating
    # simulates "this was scheduled for earlier tonight and never got a
    # response" without literally waiting hours.
    tz = ZoneInfo(user.timezone)
    now_local = datetime.now(tz)
    slides.start_time = now_local - timedelta(minutes=70)
    slides.end_time = now_local - timedelta(minutes=50)
    db.commit()
    print(f"  backdated: start={slides.start_time} end={slides.end_time}")

    section(f"Backdating the important task to simulate its window having actually passed (id={proposal.id})")
    # confirm_plan placed it moments ago, stacked right after the other
    # tasks just added - real elapsed time since then is only seconds, not
    # enough for a genuine "did you get to it" to make sense. Manually
    # firing the check-in (below) is the sanctioned stand-in for waiting on
    # real clock time (per this day's own instructions), but that alone
    # doesn't simulate the item's scheduled window having actually elapsed -
    # backdating it directly is what makes the reschedule below meaningful
    # rather than a same-slot no-op (which is what it correctly did on the
    # first pass at this test, before this step was added: with nothing
    # actually elapsed, the exact same earliest gap still won).
    tz = ZoneInfo(user.timezone)
    now_local = datetime.now(tz)
    proposal.start_time = now_local - timedelta(minutes=40)
    proposal.end_time = now_local - timedelta(minutes=10)
    db.commit()
    print(f"  backdated: start={proposal.start_time} end={proposal.end_time}")

    section(f"Completion check-in fires for the important task (id={proposal.id})")
    cli.dispatch_command(sess, f":checkin {proposal.id}")
    db.refresh(proposal)
    assert proposal.checkin_waiting is True, "check-in fire should have set checkin_waiting"

    section(f"Answer 'no, didn't get to it' -> reschedule_confirmed_item (id={proposal.id})")
    old_proposal_start = proposal.start_time
    cli.dispatch_command(sess, f":answer_checkin {proposal.id} no")
    db.refresh(proposal)
    assert proposal.checkin_waiting is False
    assert proposal.important is True, "important must survive the reschedule (this was a real Day 5 bug)"
    assert proposal.start_time != old_proposal_start, "should have moved to a new slot"
    assert proposal.start_time.astimezone(tz) >= now_local, "must never reschedule into the past (Day 5's min_start fix)"
    print(f"  proposal rescheduled: {old_proposal_start} -> {proposal.start_time}")

    section("Evening check-in fires - should sweep up everything still pending and already past")
    cli.dispatch_command(sess, ":evening")
    db.refresh(slides)
    db.refresh(quickcall)
    db.refresh(teamsync)
    db.refresh(proposal)
    print(f"  slides.evening_checkin_flagged   = {slides.evening_checkin_flagged}")
    print(f"  quickcall.evening_checkin_flagged = {quickcall.evening_checkin_flagged}")
    print(f"  teamsync.evening_checkin_flagged  = {teamsync.evening_checkin_flagged}")
    print(f"  proposal.evening_checkin_flagged  = {proposal.evening_checkin_flagged} (should be False - "
          f"rescheduled to a future slot by its own check-in already, not 'left over')")
    assert slides.evening_checkin_flagged is True
    assert quickcall.evening_checkin_flagged is True
    assert teamsync.evening_checkin_flagged is True
    assert proposal.evening_checkin_flagged is False

    section(f"Defer 'prep slides' -> keep_pending (id={slides.id})")
    cli.dispatch_command(sess, f":answer_evening {slides.id} keep_pending")
    db.refresh(slides)
    assert slides.evening_checkin_flagged is False
    assert slides.status == "pending"
    print(f"  slides left exactly where it was: start={slides.start_time}")

    choose_target = today + timedelta(days=3)
    section(f"Defer 'quick call' -> choose_date {choose_target} (id={quickcall.id})")
    cli.dispatch_command(sess, f":answer_evening {quickcall.id} choose_date {choose_target.isoformat()}")
    db.refresh(quickcall)
    assert quickcall.evening_checkin_flagged is False
    assert quickcall.start_time.date() == choose_target
    print(f"  quickcall moved to: {quickcall.start_time}")

    section(f"Defer 'team sync' -> tomorrow (id={teamsync.id})")
    cli.dispatch_command(sess, f":answer_evening {teamsync.id} tomorrow")
    db.refresh(teamsync)
    assert teamsync.evening_checkin_flagged is False
    assert teamsync.start_time.date() == today + timedelta(days=1)
    print(f"  teamsync moved to: {teamsync.start_time}")

    section("Final item state")
    cli.dispatch_command(sess, ":items")

    db.close()
    print("\nDay 7 full-loop integration test: ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
