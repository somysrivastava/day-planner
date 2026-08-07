import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services.calendar import create_event, list_upcoming_events  # noqa: E402

USER_ID = 1


def main():
    db = SessionLocal()

    print("Fetching your upcoming events...\n")
    events = list_upcoming_events(db, USER_ID, max_results=10)

    if not events:
        print("No upcoming events found.")
    else:
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            print(f"- {event.get('summary', '(no title)')}  ({start})")

    print("\nCreating a test event one hour from now...")
    user = db.get(User, USER_ID)
    time_zone = user.timezone

    start = datetime.now().astimezone() + timedelta(hours=1)
    end = start + timedelta(minutes=30)

    created = create_event(
        db,
        USER_ID,
        summary="Day Planner test event (Python)",
        description="Created by scripts/test_calendar.py to verify write access.",
        start_datetime=start.isoformat(),
        end_datetime=end.isoformat(),
        time_zone=time_zone,
    )

    print(f"Created: {created['summary']}")
    print(f"View it: {created['htmlLink']}")

    db.close()


if __name__ == "__main__":
    main()
