from __future__ import annotations

from datetime import date as date_
from datetime import datetime
from datetime import time as time_
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Item, User
from app.services.scheduler import get_effective_schedule
from app.services.tts import synthesize_speech

MODEL = "gpt-4o-mini"

BRIEFING_SYSTEM_PROMPT = """You are writing the script for a short WhatsApp voice note: a warm, personal morning briefing of the user's day. You are a companion who knows their day, not a robot reading a list back to them.

You'll be given the day's schedule (fixed events and already-placed tasks, chronological) and any reminders due today. Turn this into natural spoken text, as if a thoughtful friend were catching them up over coffee:
- Open with a brief, warm greeting - vary it, don't reuse the same opener every time.
- Walk through the day's shape in flowing sentences, not a read-aloud list - group related back-to-back items naturally, mention approximate times the way a person would ("first up at 5, then office from 10 through the evening") rather than every single "HH:MM to HH:MM".
- Birthdays, anniversaries, celebrations: don't just state the fact ("it's their birthday") - give it a genuine, warm nudge to actually do something about it, like reaching out or wishing them ("worth sending them a message today" / "make sure you catch them for a call"). Celebratory, specific, not a throwaway mention.
- Big or high-stakes meetings: give it an encouraging, steadying note ("hope that goes well," "you've got this") - confident in them, never anxious or ominous framing that makes it sound scarier than it is.
- Birthdays and big meetings are "the one thing that matters" - they should clearly stand out from the rest of the day, not get buried in the list - but keep it to a sentence or two each, not the centerpiece of the whole briefing.
- Reminders due today (no time slot - some are one-off, some recur monthly, but both should sound identical in tone, never mechanically flagged as "recurring" or "this repeats"): weave them into the natural flow near the end as things to keep in mind today, not appended as a separate list, and don't imply they're scheduled at a specific time.
- If the day is empty or very light, say so warmly ("today's wide open") rather than awkwardly stating "no events."
- Close with a brief, natural sign-off. No "let me know if you need anything else" AI-assistant phrasing.
- Tone discipline, above everything else here: warm, never saccharine or performative. You inform, you don't enforce - never imply obligation, guilt, or pressure about anything on the day (an unattended reminder, an approaching deadline, a skipped habit). Match energy to what's actually there - an ordinary day doesn't need forced enthusiasm, and a birthday doesn't need restraint, but nothing here is ever a "you'd better..." or "don't forget or else."
- Keep it tight: this becomes a spoken voice note, not an essay - aim for roughly 30-45 seconds of natural speech (about 90-140 words). Never use markdown, bullet points, or asterisks - this is spoken text only.
"""


def _reminder_occurs_on(reminder: Item, target_date: date_, tz: ZoneInfo) -> bool:
    """True if this reminder should surface in target_date's briefing.
    Non-recurring: exact match against its single stored date, as before.
    Recurring (Week 2 Day 1): RRULE-expanded the same way
    scheduler.get_effective_schedule expands fixed_event occurrences -
    duplicated here as a small local helper rather than importing
    scheduler's private _occurs_on, since the two only share ~4 lines."""
    anchor_local = reminder.start_time.astimezone(tz).replace(tzinfo=None)
    if not reminder.recurrence_rule:
        return anchor_local.date() == target_date
    rule = rrulestr(reminder.recurrence_rule, dtstart=anchor_local)
    day_start = datetime.combine(target_date, time_.min)
    day_end = datetime.combine(target_date, time_.max)
    return len(rule.between(day_start, day_end, inc=True)) > 0


def _describe_day(db: Session, user_id: int, target_date: date_, tz: ZoneInfo) -> str:
    blocks = get_effective_schedule(db, user_id, target_date)

    all_reminders = (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.type == "reminder", Item.status != "cancelled")
        .all()
    )
    reminders_today = [r for r in all_reminders if _reminder_occurs_on(r, target_date, tz)]

    lines = [f"Date: {target_date.strftime('%A, %B %d, %Y')}"]
    if blocks:
        lines.append("Schedule (chronological):")
        for b in blocks:
            lines.append(f"- {b.start.strftime('%H:%M')}-{b.end.strftime('%H:%M')}: {b.title} ({b.source})")
    else:
        lines.append("Schedule: nothing on the calendar - the day is wide open.")

    if reminders_today:
        lines.append("Reminders due today (no time slot):")
        for r in reminders_today:
            lines.append(f"- {r.title}")

    return "\n".join(lines)


def generate_briefing_text(db: Session, user_id: int, target_date: date_) -> str:
    """Part 3, text half: turns a user's effective schedule for target_date
    (reusing scheduler.get_effective_schedule, same source of truth the
    auto-fit engine itself uses) plus any reminders due that day into warm,
    natural briefing text via the LLM - this is where the 'companion, not
    robot' tone differentiator (see docs/pitch.md) actually lives."""
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    tz = ZoneInfo(user.timezone)

    day_summary = _describe_day(db, user_id, target_date, tz)

    client = OpenAI(api_key=settings.openai_api_key)
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": BRIEFING_SYSTEM_PROMPT},
            {"role": "user", "content": day_summary},
        ],
    )
    return completion.choices[0].message.content


BRIEFING_SPEAKING_RATE = 1.2  # slightly brisker than TTS default 1.0 - a full-pace read of a day's plan


def generate_voice_briefing(
    db: Session, user_id: int, target_date: date_, output_path: Optional[str] = None
) -> tuple[str, bytes]:
    """Part 3, end-to-end: text half piped through tts.synthesize_speech.
    Returns (briefing_text, audio_bytes) - the text is returned alongside
    the audio since it's useful on its own (e.g. a text fallback) and for
    verifying what was actually said without needing to listen back."""
    text = generate_briefing_text(db, user_id, target_date)
    audio = synthesize_speech(text, output_path=output_path, speaking_rate=BRIEFING_SPEAKING_RATE)
    return text, audio
