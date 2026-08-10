import sys
from datetime import date, time, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402
from app.services.briefing import generate_voice_briefing  # noqa: E402
from app.services.stt import transcribe_audio  # noqa: E402
from app.services.tts import synthesize_speech  # noqa: E402

SPARSE = "+910000000001"
PACKED = "+910000000002"

OUT_DIR = Path(__file__).resolve().parent.parent / "scratch" / "voice_test_output"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def user_id(db, phone):
    return db.query(User).filter(User.phone_number == phone).first().id


def stt_round_trip():
    """Closes the STT test loop without needing a user-recorded audio file:
    generate a known sentence via TTS, transcribe it back via STT, and
    confirm the round trip is faithful."""
    print("=" * 70)
    print("STT round-trip test (TTS-generated audio -> Groq Whisper)")
    print("=" * 70)
    original = "Reminder: pick up dry cleaning and call the dentist before five p.m."
    audio_path = OUT_DIR / "stt_input.mp3"
    synthesize_speech(original, output_path=audio_path)
    print(f"Generated test audio: {audio_path}")

    transcript = transcribe_audio(audio_path)
    print(f"Original:   {original!r}")
    print(f"Transcript: {transcript!r}")
    assert transcript.strip(), "transcription came back empty"
    # Not an exact-match check: Whisper reliably renders spoken numbers as
    # digits ("five" -> "5") and occasionally drops a short label-like
    # lead-in word before a colon-pause (verified as genuine model
    # non-determinism, not a bug in our wrapper - see PROGRESS.md). What
    # actually matters is that the substantive content words survive.
    required = ["pick up", "dry cleaning", "dentist"]
    transcript_lower = transcript.lower()
    missing = [phrase for phrase in required if phrase not in transcript_lower]
    print(f"Required content phrases missing: {missing or 'none'}")
    assert not missing, f"transcription lost real content: {missing}"


def add_temp_reminder(db, uid, target_date, title):
    row = Item(
        user_id=uid,
        type="reminder",
        title=title,
        start_time=datetime.combine(target_date, time.min).astimezone(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def cleanup_temp_reminder(db, item_id):
    row = db.get(Item, item_id)
    if row is not None:
        db.delete(row)
        db.commit()


def briefing_for(db, phone, label, target_date, with_birthday_reminder=False):
    print("\n" + "=" * 70)
    print(f"Briefing test: {label} ({phone}) on {target_date}")
    print("=" * 70)
    uid = user_id(db, phone)

    temp_reminder_id = None
    if with_birthday_reminder:
        row = add_temp_reminder(db, uid, target_date, "Mom's birthday")
        temp_reminder_id = row.id

    try:
        out_path = OUT_DIR / f"briefing_{phone.strip('+')}_{target_date.isoformat()}.mp3"
        text, audio = generate_voice_briefing(db, uid, target_date, output_path=out_path)
        print(f"\nBriefing text:\n{text}\n")
        print(f"Audio: {out_path} ({len(audio)} bytes)")
        assert len(audio) > 0
        assert out_path.exists()
        return out_path
    finally:
        if temp_reminder_id is not None:
            cleanup_temp_reminder(db, temp_reminder_id)


if __name__ == "__main__":
    stt_round_trip()

    db = SessionLocal()
    target = date(2026, 8, 12)  # Wednesday - SPARSE has 1 block, PACKED has 6
    paths = []
    paths.append(briefing_for(db, PACKED, "tightly packed", target))
    paths.append(briefing_for(db, SPARSE, "sparse day", target, with_birthday_reminder=True))
    db.close()

    print("\n" + "=" * 70)
    print("All generated audio files:")
    for p in paths:
        print(f"  {p}")
    print("=" * 70)
