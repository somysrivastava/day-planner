"""Week 2 Day 4: logging & observability. Exercises each of the four
failure points named in the task and asserts a log record actually fired,
at the right level, with the right context - not just that the code didn't
crash (or, for the calendar-sync case, that it correctly distinguishes the
expected no-account skip from a genuine failure).
"""

import logging
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  - runs logging_config as an import side effect, same as any real entry point

from app.db.base import SessionLocal  # noqa: E402
from app.db.models import Item, User  # noqa: E402
from app.services import orchestrator  # noqa: E402
from app.services import parser  # noqa: E402
from app.services import scheduler_jobs  # noqa: E402
from app.services.briefing import generate_briefing_text, generate_voice_briefing  # noqa: E402

SPARSE = "+910000000001"
PACKED = "+910000000002"  # no connected Google account either, but distinct from SPARSE for clarity


def _module_level_bad_job():
    """Must be a real module-level function, not a nested/lambda one -
    APScheduler's SQLAlchemyJobStore pickles jobs by module:function
    reference (see PROGRESS.md's pickle-safety gotcha)."""
    raise RuntimeError("simulated scheduled job failure")


@contextmanager
def capture(logger_name):
    """Attaches a plain list-appending handler to `logger_name` for the
    duration of the block, so we can assert on what actually got logged -
    not just eyeball stderr output."""
    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = ListHandler()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def user_id(db, phone):
    return db.query(User).filter(User.phone_number == phone).first().id


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def test_calendar_sync_no_account_logs_info():
    section("1a. Calendar sync - expected 'no account connected' logs at INFO, not WARNING/ERROR")
    db = SessionLocal()
    uid = user_id(db, SPARSE)  # SPARSE has no connected Google account
    with capture("app.services.orchestrator") as records:
        result = orchestrator._sync_to_calendar_best_effort(
            db, uid, summary="test event", description="test", start_datetime="2026-08-12T10:00:00",
            end_datetime="2026-08-12T10:30:00", time_zone="Asia/Kolkata",
        )
    db.close()
    assert result is None
    assert len(records) == 1, f"expected exactly 1 log record, got {len(records)}"
    assert records[0].levelno == logging.INFO, f"expected INFO, got {records[0].levelname}"
    assert "no connected" in records[0].getMessage().lower()
    print(f"  logged correctly: [{records[0].levelname}] {records[0].getMessage()}")


def test_calendar_sync_genuine_failure_logs_warning():
    section("1b. Calendar sync - a genuine failure (not 'no account') logs at WARNING with the real exception")
    db = SessionLocal()
    uid = user_id(db, PACKED)
    with capture("app.services.orchestrator") as records:
        with patch("app.services.orchestrator.calendar.create_event", side_effect=RuntimeError("simulated Google API error")):
            result = orchestrator._sync_to_calendar_best_effort(
            db, uid, summary="test event", description="test", start_datetime="2026-08-12T10:00:00",
            end_datetime="2026-08-12T10:30:00", time_zone="Asia/Kolkata",
        )
    db.close()
    assert result is None
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING, f"expected WARNING, got {records[0].levelname}"
    assert records[0].exc_info is not None, "should include the real exception, not just a message"
    print(f"  logged correctly: [{records[0].levelname}] {records[0].getMessage()}")
    print(f"  exc_info captured: {records[0].exc_info[1]!r}")


def test_briefing_text_failure_logs_and_reraises():
    section("2a. Briefing text generation - LLM call failure logs at ERROR and still raises")
    db = SessionLocal()
    uid = user_id(db, SPARSE)
    with capture("app.services.briefing") as records:
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = RuntimeError("simulated OpenAI outage")
        with patch("app.services.briefing.OpenAI", return_value=fake_client):
            try:
                generate_briefing_text(db, uid, date(2026, 8, 12))
                raised = False
            except RuntimeError:
                raised = True
    db.close()
    assert raised, "should still raise - logging must not silently swallow the failure"
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None
    print(f"  logged correctly and re-raised: [{records[0].levelname}] {records[0].getMessage()}")


def test_briefing_tts_failure_logs_and_reraises():
    section("2b. Briefing voice generation - TTS call failure logs at ERROR and still raises")
    db = SessionLocal()
    uid = user_id(db, SPARSE)
    with capture("app.services.briefing") as records:
        with patch("app.services.briefing.synthesize_speech", side_effect=RuntimeError("simulated TTS outage")):
            try:
                generate_voice_briefing(db, uid, date(2026, 8, 12))
                raised = False
            except RuntimeError:
                raised = True
    db.close()
    assert raised
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "TTS" in records[0].getMessage()
    print(f"  logged correctly and re-raised: [{records[0].levelname}] {records[0].getMessage()}")


def test_parser_call_failure_logs_and_reraises():
    section("3a. Parser - the LLM call itself failing logs at ERROR and still raises")
    with capture("app.services.parser") as records:
        fake_client = MagicMock()
        fake_client.chat.completions.parse.side_effect = RuntimeError("simulated OpenAI outage")
        with patch("app.services.parser.OpenAI", return_value=fake_client):
            try:
                parser.parse_message("prep slides, 20 minutes", reference_date=date(2026, 8, 12))
                raised = False
            except RuntimeError:
                raised = True
    assert raised
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    print(f"  logged correctly and re-raised: [{records[0].levelname}] {records[0].getMessage()}")


def test_parser_refusal_logs_and_raises():
    section("3b. Parser - an outright model refusal (no structured data at all) logs at ERROR")
    with capture("app.services.parser") as records:
        fake_message = MagicMock()
        fake_message.refusal = "simulated refusal - can't help with that"
        fake_completion = MagicMock()
        fake_completion.choices = [MagicMock(message=fake_message)]
        fake_client = MagicMock()
        fake_client.chat.completions.parse.return_value = fake_completion
        with patch("app.services.parser.OpenAI", return_value=fake_client):
            try:
                parser.parse_message("anything", reference_date=date(2026, 8, 12))
                raised = False
            except ValueError:
                raised = True
    assert raised
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "refus" in records[0].getMessage().lower()
    print(f"  logged correctly and raised: [{records[0].levelname}] {records[0].getMessage()}")


def test_nudge_fire_failure_logs_and_reraises():
    section("4a. _fire_nudge - a genuine error during firing logs at ERROR and still raises")
    with capture("app.services.scheduler_jobs") as records:
        fake_db = MagicMock()
        fake_db.get.side_effect = RuntimeError("simulated DB error")
        with patch("app.services.scheduler_jobs.SessionLocal", return_value=fake_db):
            try:
                scheduler_jobs._fire_nudge(999999)
                raised = False
            except RuntimeError:
                raised = True
    assert raised
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "999999" in records[0].getMessage()
    print(f"  logged correctly and re-raised: [{records[0].levelname}] {records[0].getMessage()}")


def test_checkin_fire_failure_logs_and_reraises():
    section("4b. _fire_checkin - a genuine error during firing logs at ERROR and still raises")
    with capture("app.services.scheduler_jobs") as records:
        fake_db = MagicMock()
        fake_db.get.side_effect = RuntimeError("simulated DB error")
        with patch("app.services.scheduler_jobs.SessionLocal", return_value=fake_db):
            try:
                scheduler_jobs._fire_checkin(999999)
                raised = False
            except RuntimeError:
                raised = True
    assert raised
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    print(f"  logged correctly and re-raised: [{records[0].levelname}] {records[0].getMessage()}")


def test_evening_checkin_fire_failure_logs_and_reraises():
    section("4c. _fire_evening_checkin - a genuine error during firing logs at ERROR and still raises")
    with capture("app.services.scheduler_jobs") as records:
        fake_db = MagicMock()
        fake_db.get.side_effect = RuntimeError("simulated DB error")
        with patch("app.services.scheduler_jobs.SessionLocal", return_value=fake_db):
            try:
                scheduler_jobs._fire_evening_checkin(999999)
                raised = False
            except RuntimeError:
                raised = True
    assert raised
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    print(f"  logged correctly and re-raised: [{records[0].levelname}] {records[0].getMessage()}")


def test_evening_checkin_missing_user_logs_warning_not_error():
    section("4d. _fire_evening_checkin - a job firing for a since-deleted user logs WARNING, doesn't crash")
    with capture("app.services.scheduler_jobs") as records:
        result = scheduler_jobs._fire_evening_checkin(999999)  # real DB, genuinely nonexistent user_id
    assert result == []
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    print(f"  logged correctly, no exception: [{records[0].levelname}] {records[0].getMessage()}")


def test_live_apscheduler_job_still_logs_via_executor():
    section("5. Sanity: a job scheduled through the real APScheduler and firing for real still surfaces the failure")
    # Not one of the four named failure points specifically, but confirms
    # the logging_config.py wiring (Week 2 Day 4's actual new piece) makes
    # APScheduler's own built-in executor error logging show up formatted
    # consistently with everything else, not just to a bare lastResort
    # handler as it did before this day's work.
    sched = scheduler_jobs.get_scheduler()
    run_at = datetime.now() + timedelta(seconds=1)

    with capture("apscheduler.executors.default") as records:
        sched.add_job(_module_level_bad_job, "date", run_date=run_at, id="test-w2d4-bad-job", replace_existing=True)
        import time

        time.sleep(3)
    assert any(r.levelno >= logging.ERROR for r in records), "APScheduler should have logged the job's exception"
    print(f"  APScheduler's own executor logged {len(records)} record(s) for the failed job")


_fired_probe_jobs: list[str] = []


def _module_level_probe_job(label: str) -> None:
    _fired_probe_jobs.append(label)


def test_misfire_grace_time_configured():
    section("6. misfire_grace_time is configured broadly (job_defaults), not APScheduler's 1-second default")
    # Found via a real leftover job silently dropped ~6 minutes overdue
    # during Week 2 Day 4's broader regression pass - APScheduler's own
    # default (verified against the installed package) is 1 second.
    sched = scheduler_jobs.get_scheduler()
    configured = sched._job_defaults.get("misfire_grace_time")
    print(f"  configured misfire_grace_time: {configured} seconds")
    assert configured == scheduler_jobs.MISFIRE_GRACE_SECONDS
    assert configured >= 1800, "should be generously more than APScheduler's bare 1-second default"


def test_misfire_grace_time_behavior():
    section("7. A moderately-late job still fires; a very-late job is still correctly dropped (and logged)")
    from apscheduler.triggers.date import DateTrigger

    sched = scheduler_jobs.get_scheduler()
    _fired_probe_jobs.clear()

    with capture("apscheduler.executors.default") as records:
        sched.add_job(
            _module_level_probe_job, DateTrigger(run_date=datetime.now() - timedelta(minutes=5)),
            args=["moderately_late"], id="test-w2d4-moderately-late", replace_existing=True,
        )
        sched.add_job(
            _module_level_probe_job, DateTrigger(run_date=datetime.now() - timedelta(hours=2)),
            args=["very_late"], id="test-w2d4-very-late", replace_existing=True,
        )
        import time

        time.sleep(3)

    print(f"  jobs that actually fired: {_fired_probe_jobs}")
    # The log message is the job's repr (function name + trigger time), not
    # its `id` string, so match on the distinguishing trigger timestamps
    # rather than a substring that's never actually present - caught by
    # running this for real and reading the actual message, not assuming
    # its format.
    missed_msgs = [r.getMessage() for r in records if "missed by" in r.getMessage()]
    print(f"  jobs logged as missed: {missed_msgs}")
    assert "moderately_late" in _fired_probe_jobs, "5 minutes overdue is within the 1-hour grace window - should still fire"
    assert "very_late" not in _fired_probe_jobs, "2 hours overdue is beyond the grace window - should be dropped, not fired late"
    assert len(missed_msgs) == 1, f"expected exactly one job (the very-late one) logged as missed, got {len(missed_msgs)}"


if __name__ == "__main__":
    test_calendar_sync_no_account_logs_info()
    test_calendar_sync_genuine_failure_logs_warning()
    test_briefing_text_failure_logs_and_reraises()
    test_briefing_tts_failure_logs_and_reraises()
    test_parser_call_failure_logs_and_reraises()
    test_parser_refusal_logs_and_raises()
    test_nudge_fire_failure_logs_and_reraises()
    test_checkin_fire_failure_logs_and_reraises()
    test_evening_checkin_fire_failure_logs_and_reraises()
    test_evening_checkin_missing_user_logs_warning_not_error()
    test_live_apscheduler_job_still_logs_via_executor()
    test_misfire_grace_time_configured()
    test_misfire_grace_time_behavior()

    print("\n" + "=" * 70)
    print("ALL LOGGING/OBSERVABILITY CHECKS PASSED")
    print("=" * 70)
