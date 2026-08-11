"""Week 2 Day 2: briefing tone refinement (birthday nudge, big-meeting
encouragement, recurring reminders reading naturally). This is a prompt
tuning pass on top of Week 1 Day 6's briefing generator - tone quality is
inherently a listening judgment (same posture as Day 6's audio-quality
check), so this script generates real audio for a few different event
mixes and plays each one aloud rather than only asserting on the text.
"""

import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402
from app.services.briefing import generate_voice_briefing  # noqa: E402

SPARSE = "+910000000001"
PACKED = "+910000000002"
NO_MORNING = "+910000000003"

TARGET = date(2026, 8, 13)  # Thursday - clean, canonical schedule for all 3 synthetic users

OUT_DIR = Path(__file__).resolve().parent.parent / "scratch" / "voice_test_output"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def user_id(db, phone):
    return db.query(User).filter(User.phone_number == phone).first().id


def add_temp_item(db, uid, **kwargs):
    row = Item(user_id=uid, **kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def cleanup_temp_item(db, item_id):
    row = db.get(Item, item_id)
    if row is not None:
        db.delete(row)
        db.commit()


def scenario(db, label, phone, temp_item_kwargs=None):
    print("\n" + "=" * 70)
    print(f"Scenario: {label} ({phone})")
    print("=" * 70)
    uid = user_id(db, phone)

    temp_id = None
    if temp_item_kwargs is not None:
        row = add_temp_item(db, uid, **temp_item_kwargs)
        temp_id = row.id

    try:
        out_path = OUT_DIR / f"tone_{label.lower().replace(' ', '_')}.mp3"
        text, audio = generate_voice_briefing(db, uid, TARGET, output_path=out_path)
        print(f"\n{text}\n")
        print(f"Audio: {out_path} ({len(audio)} bytes)")
        assert len(audio) > 0
        return out_path
    finally:
        if temp_id is not None:
            cleanup_temp_item(db, temp_id)


def main():
    db = SessionLocal()
    paths = []

    # 1. Birthday day - one-off reminder, otherwise a light day.
    paths.append(
        scenario(
            db, "Birthday day", SPARSE,
            temp_item_kwargs=dict(
                type="reminder", title="Mom's birthday",
                start_time=datetime.combine(TARGET, time.min).astimezone(),
            ),
        )
    )

    # 2. Big/important meeting day - explicit-time task with an unambiguously
    # high-stakes title, dropped into a free slot (10-11am, before Evening Class).
    paths.append(
        scenario(
            db, "Big meeting day", NO_MORNING,
            temp_item_kwargs=dict(
                type="task", title="Final interview for the VP of Product role", status="pending",
                start_time=datetime.combine(TARGET, time(10, 0)).astimezone(),
                end_time=datetime.combine(TARGET, time(11, 0)).astimezone(),
            ),
        )
    )

    # 3. Recurring reminder day - monthly rent reminder, dropped into an
    # already-busy day (PACKED) to check it doesn't feel bolted onto a full schedule.
    paths.append(
        scenario(
            db, "Recurring reminder day", PACKED,
            temp_item_kwargs=dict(
                type="reminder", title="Rent due", recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=13",
                start_time=datetime.combine(TARGET, time.min).astimezone(),
            ),
        )
    )

    # 4. Plain ordinary day - no temp items at all, just SPARSE's real seed schedule.
    paths.append(scenario(db, "Plain ordinary day", SPARSE, temp_item_kwargs=None))

    db.close()

    print("\n" + "=" * 70)
    print("All generated audio files:")
    for p in paths:
        print(f"  {p}")
    print("=" * 70)


if __name__ == "__main__":
    main()
