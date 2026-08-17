"""Persistent, REPL-style stand-in for the WhatsApp thread (Day 7). Type
free-form messages like a real user; :-prefixed commands simulate the
time-triggered/manual-reply parts of the flow (briefing, nudges, check-ins)
that would otherwise require waiting on real clock time. Run with:

    python3 scripts/cli.py
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402
from app.services import scheduler_jobs  # noqa: E402
from app.services.briefing import generate_voice_briefing  # noqa: E402
from app.services.orchestrator import (  # noqa: E402
    Plan,
    build_plan,  # noqa: F401  (kept available for direct use if a future command needs it)
    confirm_plan,
    remove_item,
    reschedule_item,
    resolve_clarification,
    resolve_collision,
    resolve_duration_question,
    resolve_month_length_warning,
    resolve_no_fit,
    resolve_reminder_clarification,
    route_message,
)
from app.services.parser import parse_message  # noqa: E402

SEED_USERS = {
    "you": "+919354211791",
    "sparse": "+910000000001",
    "packed": "+910000000002",
    "nomorning": "+910000000003",
}

HELP = """
Type a free-form message to send it through parse -> route_message -> build_plan,
same as a real WhatsApp message. You'll be walked through any pending questions
(duration, collision, no-fit, vague clarification) right here, then asked to
confirm.

Commands:
  :help                          show this
  :switch <you|sparse|packed|nomorning|+91...>   change the active test user
  :items                         list this user's items (ids needed below)
  :confirm [YYYY-MM-DD]          re-open the yes/no confirm loop for a pending plan (default: today)
  :briefing                      generate + print today's morning briefing (manual trigger)
  :evening                       manually fire tonight's evening check-in sweep now
  :nudge <item_id>                manually fire that item's pre-task nudge now
  :checkin <item_id>              manually fire that item's completion check-in now
  :answer_checkin <item_id> yes|no
  :answer_evening <item_id> tomorrow|choose_date|keep_pending [YYYY-MM-DD]
  :quit                          exit
""".strip()


class Session:
    def __init__(self, db, user: User):
        self.db = db
        self.user = user
        self.pending_plans: dict[date, Plan] = {}


def get_user(db, key: str) -> User:
    phone = SEED_USERS.get(key.strip().lower(), key.strip())
    user = db.query(User).filter(User.phone_number == phone).first()
    if user is None:
        raise ValueError(f"No user found for {key!r} (try you/sparse/packed/nomorning, or a phone number)")
    # Registers/refreshes this user's daily evening-check-in job. There's no
    # app-startup hook yet that does this for every user (no long-running
    # server process exists at all currently - same deferred-deployment gap
    # already flagged for Day 5's nudge/check-in jobs) - selecting a user
    # here is the stand-in for that until Week 2/3 deployment work.
    scheduler_jobs.schedule_evening_checkin(
        user.id, user.evening_checkin_time.hour, user.evening_checkin_time.minute, user.timezone
    )
    return user


def show_items(sess: Session) -> None:
    rows = (
        sess.db.query(Item)
        .filter(Item.user_id == sess.user.id, Item.status != "cancelled")
        .order_by(Item.start_time)
        .all()
    )
    if not rows:
        print("  (no items)")
    for r in rows:
        flags = []
        if r.important:
            flags.append("important")
        if r.checkin_waiting:
            flags.append("checkin_waiting")
        if r.evening_checkin_flagged:
            flags.append("evening_flagged")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"  id={r.id:<4} {r.type:<12} {r.status:<9} {r.start_time} -> {r.end_time}  {r.title!r}{flag_str}")


def resolve_pending_interactively(db, plan: Plan) -> None:
    while plan.has_pending_issues:
        if plan.pending_duration_questions:
            t = plan.pending_duration_questions[0]
            minutes = input(f"  How long do you think '{t.title}' will take (minutes)? ").strip()
            resolve_duration_question(db, plan, t, int(minutes))
        elif plan.pending_clarifications:
            t = plan.pending_clarifications[0]
            when = input(f"  When should '{t.title}' happen? (today/tomorrow/YYYY-MM-DD) ").strip().lower()
            if when == "today":
                day = date.today()
            elif when == "tomorrow":
                day = date.today() + timedelta(days=1)
            else:
                day = date.fromisoformat(when)
            tod = input("  Time of day preference (morning/afternoon/evening, blank = none)? ").strip().lower() or None
            minutes = input(f"  How long will '{t.title}' take (minutes)? ").strip()
            note = resolve_clarification(db, plan, t, day, int(minutes), tod)
            if note:
                print(f"  ({note})")
        elif plan.pending_collisions:
            c = plan.pending_collisions[0]
            choice = input(
                f"  '{c.new_item_title}' collides with existing '{c.colliding_task_title}' at that time - "
                f"move the existing one, or keep this one elsewhere? (move/keep) "
            ).strip().lower()
            resolve_collision(db, plan, c, "move_existing" if choice.startswith("m") else "keep_new_elsewhere")
        elif plan.pending_no_fits:
            nf = plan.pending_no_fits[0]
            choice = input(
                f"  No fit for '{nf.item_title}' today ({nf.reason}) - shift to tomorrow, or force it in today? "
                f"(tomorrow/force) "
            ).strip().lower()
            note = resolve_no_fit(db, plan, nf, "shift_to_tomorrow" if choice.startswith("t") else "force_today")
            if note:
                print(f"  ({note})")
        elif plan.pending_month_length_warnings:
            w = plan.pending_month_length_warnings[0]
            choice = input(
                f"  '{w.reminder.title}' ({w.reminder.date}) is anchored on the 29th-31st and won't fire in "
                f"shorter months - keep as-is, pick a different day, or always use the last day of the month? "
                f"(keep/different/last) "
            ).strip().lower()
            if choice.startswith("d"):
                new_day = input("  New day of month (1-28)? ").strip()
                resolve_month_length_warning(plan, w, "different_day", new_day=int(new_day))
            elif choice.startswith("l"):
                resolve_month_length_warning(plan, w, "last_day_of_month")
            else:
                resolve_month_length_warning(plan, w, "keep_as_is")
        elif plan.pending_reminder_clarifications:
            r = plan.pending_reminder_clarifications[0]
            date_str = input(f"  '{r.title}' - what date did you actually mean (YYYY-MM-DD)? ").strip()
            ldm = input("  Should this always be the last day of the month? (y/n) ").strip().lower().startswith("y")
            resolve_reminder_clarification(plan, r, date_str, last_day_of_month=ldm)


def confirm_now(sess: Session, d: date) -> None:
    plan = sess.pending_plans.get(d)
    if plan is None:
        print(f"No pending plan for {d}.")
        return

    while True:
        print(f"\n--- Plan for {d} ---")
        print(plan.summary_text())
        if plan.has_pending_issues:
            resolve_pending_interactively(sess.db, plan)
            continue

        ans = input("Confirm this plan? (yes/no) ").strip().lower()
        if ans in ("y", "yes"):
            saved = confirm_plan(sess.db, plan)
            print(f"Confirmed - {len(saved)} item(s) saved:")
            for row in saved:
                print(f"  id={row.id} {row.title!r} start={row.start_time} important={row.important}")
            del sess.pending_plans[d]
            return
        elif ans in ("n", "no"):
            change = input("What would you like changed? (drop <title> / move <title> / done) ").strip()
            if not change or change.lower() == "done":
                print("Leaving this plan unconfirmed for now.")
                return
            action, _, rest = change.partition(" ")
            title = rest.strip()
            if action == "drop":
                print("Dropped." if remove_item(plan, title) else "Not found in this plan.")
            elif action == "move":
                minutes = input("  New duration in minutes (blank = keep current)? ").strip()
                tod = input("  New time-of-day preference (morning/afternoon/evening, blank = none)? ").strip().lower()
                result = reschedule_item(sess.db, plan, title, int(minutes) if minutes else None, tod or None)
                if result:
                    print(f"  Still no fit: {result.reason}")
            else:
                print(f"Unrecognized action {action!r}. Try 'drop <title>' or 'move <title>'.")
        else:
            print("Please answer yes or no.")


def handle_message(sess: Session, text: str) -> None:
    parsed = parse_message(text, reference_date=date.today(), timezone=sess.user.timezone)
    plans = route_message(sess.db, sess.user.id, date.today(), parsed)
    for d, plan in sorted(plans.items()):
        sess.pending_plans[d] = plan
        confirm_now(sess, d)


def dispatch_command(sess: Session, line: str) -> bool:
    """Returns False to quit the REPL."""
    parts = line[1:].split()
    if not parts:
        return True
    cmd, *args = parts

    if cmd in ("q", "quit", "exit"):
        return False
    elif cmd == "help":
        print(HELP)
    elif cmd == "switch":
        if not args:
            print("Usage: :switch <you|sparse|packed|nomorning|+91...>")
        else:
            sess.user = get_user(sess.db, args[0])
            sess.pending_plans.clear()
            print(f"Switched to {sess.user.phone_number} (id={sess.user.id})")
    elif cmd == "items":
        show_items(sess)
    elif cmd == "confirm":
        d = date.fromisoformat(args[0]) if args else date.today()
        confirm_now(sess, d)
    elif cmd == "briefing":
        text, _audio = generate_voice_briefing(sess.db, sess.user.id, date.today())
        print(f"\n[BRIEFING]\n{text}\n")
    elif cmd == "evening":
        scheduler_jobs._fire_evening_checkin(sess.user.id)
    elif cmd == "nudge":
        if not args:
            print("Usage: :nudge <item_id>")
        else:
            scheduler_jobs._fire_nudge(int(args[0]))
    elif cmd == "checkin":
        if not args:
            print("Usage: :checkin <item_id>")
        else:
            scheduler_jobs._fire_checkin(int(args[0]))
    elif cmd == "answer_checkin":
        if len(args) < 2:
            print("Usage: :answer_checkin <item_id> yes|no")
        else:
            item_id, answer = int(args[0]), args[1].lower()
            result = scheduler_jobs.answer_checkin(sess.db, item_id, done=(answer == "yes"))
            if answer == "yes":
                print("Acknowledged, marked done.")
            elif result is None:
                print("Rescheduled to a new slot.")
            else:
                print(f"Could not reschedule: {result.reason}")
    elif cmd == "answer_evening":
        if len(args) < 2:
            print("Usage: :answer_evening <item_id> tomorrow|choose_date|keep_pending [YYYY-MM-DD]")
        else:
            item_id, choice = int(args[0]), args[1].lower()
            chosen_date = date.fromisoformat(args[2]) if len(args) > 2 else None
            result = scheduler_jobs.answer_evening_checkin(sess.db, item_id, choice, chosen_date)
            if choice == "keep_pending":
                print("Left as pending.")
            elif result is None:
                print("Deferred successfully.")
            else:
                print(f"Could not defer: {result.reason}")
    else:
        print(f"Unknown command: {cmd}. Type :help.")
    return True


def run() -> None:
    print("Day Planner CLI stub. Type :help for commands.")
    db = SessionLocal()
    key = input("Which user? (you/sparse/packed/nomorning or phone) ").strip() or "you"
    user = get_user(db, key)
    sess = Session(db, user)
    print(f"Active user: {user.phone_number} (id={user.id}, tz={user.timezone})")

    while True:
        try:
            line = input(f"[{sess.user.phone_number}] > ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line.startswith(":"):
            if not dispatch_command(sess, line):
                break
        else:
            handle_message(sess, line)

    db.close()
    print("Bye.")


if __name__ == "__main__":
    run()
