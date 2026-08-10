from __future__ import annotations

from datetime import date as date_
from typing import Optional
from zoneinfo import ZoneInfo

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
- If anything in the schedule or reminders sounds like a birthday, anniversary, celebration, or a notably big/high-stakes meeting, call it out specifically with matching tone (celebratory for a birthday, encouraging/steadying for a big meeting) - this is the one thing that should stand out, not get buried in the list.
- Mention reminders due today near the end, briefly - these have no time slot, so don't imply they're scheduled.
- If the day is empty or very light, say so warmly ("today's wide open") rather than awkwardly stating "no events."
- Close with a brief, natural sign-off. No "let me know if you need anything else" AI-assistant phrasing.
- Keep it tight: this becomes a spoken voice note, not an essay - aim for roughly 30-45 seconds of natural speech (about 90-140 words). Never use markdown, bullet points, or asterisks - this is spoken text only.
"""


def _to_local_date(dt, tz: ZoneInfo) -> date_:
    return dt.astimezone(tz).replace(tzinfo=None).date()


def _describe_day(db: Session, user_id: int, target_date: date_, tz: ZoneInfo) -> str:
    blocks = get_effective_schedule(db, user_id, target_date)

    reminders_today = (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.type == "reminder", Item.status != "cancelled")
        .all()
    )
    reminders_today = [r for r in reminders_today if _to_local_date(r.start_time, tz) == target_date]

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
