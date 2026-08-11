from __future__ import annotations

import datetime as dt
from typing import List, Literal, Optional, Union

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from app.config import settings

MODEL = "gpt-4o-mini"

WeekdayCode = Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
TimeOfDay = Literal["morning", "afternoon", "evening"]


class ReminderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["reminder"]
    title: str
    date: str  # ISO 8601 date this reminder fires on (or, if recurring, its next/anchor occurrence)
    recurring: bool = False  # true for "every month on the Nth" - day-of-month is derived from `date`
    last_day_of_month: bool = False  # true for "the last day of every month" - a distinct recurrence, not a fallback; implies recurring=True. `date` is corrected deterministically downstream regardless of what's set here, so an approximate guess is fine.


class TaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["task"]
    title: str
    day: Optional[str] = None  # ISO 8601 date, if stated or inferable
    duration_minutes: Optional[int] = None
    time_of_day_preference: Optional[TimeOfDay] = None
    urgent: bool = False
    important: bool = False  # opt-in completion check-in after scheduled time passes (Day 5)
    needs_clarification: bool = False  # true only if BOTH day and duration are absent


class RecurringTime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: WeekdayCode
    time: Optional[str] = None  # "HH:MM" 24-hour; null if this day's time still needs asking


class RecurringTaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["recurring_task"]
    title: str
    days: List[WeekdayCode]
    duration_minutes: Optional[int] = None
    times: List[RecurringTime] = []
    time_of_day_preference: Optional[TimeOfDay] = None
    urgent: bool = False


class ExplicitTimeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["explicit_time_item"]
    title: str
    date: str  # ISO 8601 date; assume the reference date if none stated
    start_time: str  # "HH:MM" 24-hour
    duration_minutes: Optional[int] = None
    important: bool = False  # opt-in completion check-in after scheduled time passes (Day 5)


ParsedItem = Union[ReminderItem, TaskItem, RecurringTaskItem, ExplicitTimeItem]


class ParsedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ParsedItem]


def _system_prompt(reference_date: dt.date, timezone: str) -> str:
    weekday = reference_date.strftime("%A")
    return f"""You are the message-parsing layer for a WhatsApp day-planner bot. Convert the user's free-form message into one or more structured items.

Reference date: {reference_date.isoformat()} ({weekday}). User's timezone: {timezone}. Resolve all relative dates ("today", "tomorrow", "next Monday") against this reference date into absolute ISO 8601 dates (YYYY-MM-DD).

Classify each distinct thing the user mentions as exactly one of:
- reminder: has a future date, no time slot.
  - Plain one-time reminder ("remind me to submit tax by the 15th"): exactly one item, recurring=false, `date` set to that date.
  - Advance notice on a one-time reminder ("remind me 3 days before and 1 day before X"): emit one item per stated interval, PLUS one additional item dated on the deadline day itself - stated-intervals + 1 total items, all recurring=false.
  - Monthly-recurring reminder, no advance notice ("rent due on the 5th every month"): exactly ONE item, recurring=true, `date` set to the next upcoming occurrence of that day-of-month (this month's if not yet passed, otherwise next month's). CRITICAL: recurring=true by itself already means "this fires every month going forward" - do NOT enumerate multiple items for multiple future months. One recurring reminder is always exactly one item, never N items for N future occurrences.
  - "Last day of every month" recurring reminder: exactly ONE item, recurring=true AND last_day_of_month=true (the exact `date` doesn't need to be precise, it's corrected deterministically downstream) - same one-item rule as above.
  - Advance notice on a monthly-recurring reminder ("remind me 3 days before rent is due every month", rent on the 5th): treat exactly like advance notice on a one-time reminder, but every resulting item ALSO gets recurring=true, and each item's `date` is that stated interval's own single anchor day-of-month (3 days before the 5th -> the 2nd) - so this example produces exactly 2 items total (5th and 2nd, both recurring=true, one item each), not one item per interval per future month. The "stated-intervals + 1 items" rule and the "one item per recurring reminder" rule combine by multiplying intervals, never by multiplying months.
- task: one-off, needs a time slot, no explicit clock time was stated. Extract duration_minutes if stated or clearly inferable. Set needs_clarification to true ONLY when BOTH the day and the duration are absent from the message. If either one is stated or inferable, set needs_clarification to false even though the other is still missing - do not guess the missing one, just leave it null.
- recurring_task: daily or day-specific repeating language ("every day", "read Mon/Wed/Fri"). If the message describes one repeating commitment, return it as a single recurring_task item, even if the time differs by day - list every applicable weekday in `days` and each day's time (or null if still needs asking) in `times`. Do not split one commitment into multiple recurring_task items.
- explicit_time_item: the user gave a specific clock time (e.g. "at 2pm", "2-3pm"). This skips all scheduling logic - record the date (assume the reference date if none stated) and time only.

Extract, only where the field applies to that type: whether the user flagged the item as urgent ("this one's urgent") - affects which gap it gets during scheduling; whether the user flagged it as important ("this is important", "mark as important", "flag this") - a completely separate concept from urgent, triggers a check-in after the scheduled time asking if it got done; and a vague time-of-day preference (morning/afternoon/evening) if stated.

If the message contains multiple distinct items, return one entry per item, in the order the user mentioned them. Each distinct real-world thing the user mentions must produce exactly ONE item, classified as whichever single type best fits - never emit two items (e.g. an explicit_time_item and a task, or two of the same type) for the same single thing just because it could arguably be read more than one way."""


def parse_message(
    text: str,
    reference_date: Optional[dt.date] = None,
    timezone: str = "Asia/Kolkata",
) -> ParsedMessage:
    # Known gpt-4o-mini limitations (see scripts/test_parser.py), accepted for now:
    # - needs_clarification can misfire on multi-item messages even when a day
    #   was inferred for that item.
    # - a single recurring commitment with different per-day times sometimes
    #   comes back split across multiple recurring_task items instead of one
    #   with several `times` entries. Data is still correct, just not merged.
    # Downstream code should tolerate both rather than assume perfect output.
    reference_date = reference_date or dt.date.today()
    client = OpenAI(api_key=settings.openai_api_key)

    completion = client.chat.completions.parse(
        model=MODEL,
        messages=[
            {"role": "system", "content": _system_prompt(reference_date, timezone)},
            {"role": "user", "content": text},
        ],
        response_format=ParsedMessage,
    )

    message = completion.choices[0].message
    if message.refusal:
        raise ValueError(f"Model refused to parse message: {message.refusal}")

    return message.parsed


def check_collision(item: ParsedItem, user_id: int) -> bool:
    """TODO(Day 4): query the items table for overlapping start/end times
    once the scheduler's DB access patterns are built. Placeholder for now."""
    raise NotImplementedError("Collision detection is implemented in the Day 4 scheduler.")
