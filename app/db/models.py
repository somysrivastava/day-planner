from datetime import date, datetime, time

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Integer, String, Text, TIMESTAMP, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False, server_default="UTC")
    briefing_time: Mapped[time] = mapped_column(Time, nullable=False, server_default="07:30:00")
    google_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    wake_time: Mapped[time] = mapped_column(Time, nullable=False, server_default="06:00:00")
    sleep_time: Mapped[time] = mapped_column(Time, nullable=False, server_default="23:00:00")
    nudge_lead_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    evening_checkin_time: Mapped[time] = mapped_column(Time, nullable=False, server_default="20:00:00")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint("type IN ('reminder', 'task', 'fixed_event')", name="items_type_check"),
        CheckConstraint("status IN ('pending', 'done', 'cancelled')", name="items_status_check"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    start_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    recurrence_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Opt-in completion check-in (Day 5): only tasks/explicit-time items the
    # user explicitly flags "important" get a check-in after their scheduled
    # time passes. checkin_waiting is true from when the check-in fires
    # until it's answered - see app/services/scheduler_jobs.py.
    important: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    checkin_waiting: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Evening check-in (Day 7): true from when the nightly sweep flags this
    # still-pending task until the user answers Tomorrow/Choose a date/leave
    # it. A separate flag from checkin_waiting - that one is the Day 5
    # single-item important-task check-in, this is the once-nightly batch
    # sweep of everything else left over. See scheduler_jobs.py.
    evening_checkin_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # APScheduler job ids so a nudge/check-in can be found and cancelled
    # if this item gets rescheduled - see reschedule_confirmed_item().
    nudge_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkin_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


# A date-specific exception to a fixed_event's recurring schedule: fully
# skipped that date, shifted to one replacement range, or split into two
# ranges (an explicit-time item colliding with the block). At most two
# segments — nothing in the spec implies more than one collision-driven
# split of the same block on the same date.
class FixedEventOverride(Base):
    __tablename__ = "fixed_event_overrides"
    __table_args__ = (UniqueConstraint("item_id", "override_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    override_date: Mapped[date] = mapped_column(Date, nullable=False)
    skip: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    segment_1_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    segment_1_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    segment_2_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    segment_2_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
