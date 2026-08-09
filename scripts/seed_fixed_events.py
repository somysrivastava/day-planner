import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402

# Anchor dates for recurring events: DTSTART needs a concrete first
# occurrence, even though RRULE's BYDAY controls which weekdays actually
# repeat. Using the next Mon/Tue from today keeps these dates sane.
MONDAY = "2026-08-10"
TUESDAY = "2026-08-11"
TZ_OFFSET = "+05:30"  # Asia/Kolkata, fixed offset (no DST)

WEEKDAYS_RULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
TUE_THU_RULE = "FREQ=WEEKLY;BYDAY=TU,TH"


def ts(date: str, time: str) -> str:
    return f"{date}T{time}:00{TZ_OFFSET}"


def upsert_user(db, phone: str, tz: str, wake_time: str = "06:00:00", sleep_time: str = "23:00:00") -> User:
    user = db.query(User).filter(User.phone_number == phone).first()
    if user:
        user.timezone = tz
        user.wake_time = wake_time
        user.sleep_time = sleep_time
    else:
        user = User(phone_number=phone, timezone=tz, wake_time=wake_time, sleep_time=sleep_time)
        db.add(user)
    db.commit()
    db.refresh(user)
    return user


# Scoped to type='fixed_event' so re-running this script never touches
# tasks/reminders that other flows may add to these same users later.
def reset_fixed_events(db, user_id: int):
    db.query(Item).filter(Item.user_id == user_id, Item.type == "fixed_event").delete(
        synchronize_session=False
    )
    db.commit()


def add_fixed_event(db, user_id: int, title: str, start_ts: str, end_ts: str, rrule: str):
    db.add(
        Item(
            user_id=user_id,
            type="fixed_event",
            title=title,
            start_time=start_ts,
            end_time=end_ts,
            recurrence_rule=rrule,
        )
    )
    db.commit()


def main():
    db = SessionLocal()

    # --- Real user: your actual routine ---
    you = upsert_user(db, "+919354211791", "Asia/Kolkata", wake_time="04:30:00")
    reset_fixed_events(db, you.id)
    add_fixed_event(db, you.id, "Gym", ts(MONDAY, "05:00"), ts(MONDAY, "07:30"), WEEKDAYS_RULE)
    add_fixed_event(db, you.id, "Office", ts(MONDAY, "10:00"), ts(MONDAY, "19:00"), WEEKDAYS_RULE)
    print(f"Seeded user {you.id} (+919354211791): Gym + Office, Mon-Fri")

    # --- Synthetic user A: sparse day, lots of free time ---
    sparse = upsert_user(db, "+910000000001", "Asia/Kolkata")
    reset_fixed_events(db, sparse.id)
    add_fixed_event(
        db, sparse.id, "Morning Standup", ts(MONDAY, "09:00"), ts(MONDAY, "09:15"), WEEKDAYS_RULE
    )
    print(f"Seeded user {sparse.id} (+910000000001, sparse day): 1 short daily event")

    # --- Synthetic user B: tightly packed, back-to-back, no gaps ---
    packed = upsert_user(db, "+910000000002", "Asia/Kolkata")
    reset_fixed_events(db, packed.id)
    packed_blocks = [
        ("Client Calls", "08:00", "10:00"),
        ("Project Work Block", "10:00", "12:30"),
        ("Lunch", "12:30", "13:00"),
        ("Team Meetings", "13:00", "15:30"),
        ("Deep Work Block", "15:30", "18:00"),
        ("Gym", "18:00", "19:30"),
    ]
    for title, start, end in packed_blocks:
        add_fixed_event(db, packed.id, title, ts(MONDAY, start), ts(MONDAY, end), WEEKDAYS_RULE)
    print(
        f"Seeded user {packed.id} (+910000000002, tightly packed): "
        f"{len(packed_blocks)} back-to-back blocks"
    )

    # --- Synthetic user C: no fixed morning routine at all ---
    no_morning = upsert_user(db, "+910000000003", "Asia/Kolkata")
    reset_fixed_events(db, no_morning.id)
    add_fixed_event(
        db, no_morning.id, "Evening Class", ts(TUESDAY, "18:30"), ts(TUESDAY, "20:00"), TUE_THU_RULE
    )
    print(f"Seeded user {no_morning.id} (+910000000003, no morning routine): 1 evening event, Tue/Thu only")

    db.close()


if __name__ == "__main__":
    main()
