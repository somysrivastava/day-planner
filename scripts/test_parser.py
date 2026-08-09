import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.parser import parse_message  # noqa: E402

# Fixed reference date so results are reproducible across runs.
# 2026-08-10 is a Monday, matching the seed data's anchor date.
REFERENCE_DATE = dt.date(2026, 8, 10)

TEST_MESSAGES = [
    "remind me to submit taxes by the 15th",
    "remind me 3 days before and 1 day before the deadline on August 20th",
    "alter clothes",
    "pick up my dress, maybe take 30 minutes",
    "read 30 min every day",
    "read Mon/Wed/Fri at 9pm but 6pm on Saturday",
    "meeting with the bank manager at 2pm",
    "call mom tomorrow evening",
    "this one's urgent - fix the leaking tap sometime today",
    "gotta submit the report by fri, need like an hour",
    "dentist appointment 3:30-4:15pm thursday",
    "I need to pick up my dress and read for 30 minutes daily starting today",
]


def main():
    for message in TEST_MESSAGES:
        print(f"\n{'=' * 70}")
        print(f"INPUT: {message}")
        print("-" * 70)
        try:
            result = parse_message(message, reference_date=REFERENCE_DATE)
        except Exception as exc:
            print(f"FAILED: {exc}")
            continue

        for item in result.items:
            print(item.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
