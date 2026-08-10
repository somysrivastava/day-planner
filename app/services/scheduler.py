from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime
from datetime import time as time_
from datetime import timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy.orm import Session

from app.db.models import FixedEventOverride, Item, User

# Morning/Afternoon/Evening windows from scheduler-algorithm.md. Evening's
# upper bound is the user's actual sleep_time, not a fixed clock time.
TIME_OF_DAY_WINDOWS: dict[str, tuple[time_, Optional[time_]]] = {
    "morning": (time_(6, 0), time_(12, 0)),
    "afternoon": (time_(12, 0), time_(17, 0)),
    "evening": (time_(17, 0), None),
}


@dataclass
class BusyBlock:
    start: datetime  # naive, user-local wall-clock
    end: datetime
    title: str
    source: Literal["fixed_event", "task"]
    item_id: Optional[int] = None


@dataclass
class FixedEventSplit:
    """A date-specific override to apply to a fixed_event's recurring
    schedule, computed but not yet written - the orchestrator persists
    this at confirm-time. Empty `segments` means the block is fully
    consumed (skip) for this date."""

    item_id: int
    title: str
    override_date: date_
    segments: list[tuple[datetime, datetime]]  # naive local; 0, 1, or 2 entries


@dataclass
class Placement:
    title: str
    start: datetime
    end: datetime
    fixed_event_splits: list[FixedEventSplit] = field(default_factory=list)
    important: bool = False  # opt-in completion check-in after scheduled time passes (Day 5)


@dataclass
class CollisionResult:
    new_item_title: str
    new_item_start: datetime
    new_item_end: datetime
    colliding_task_id: int
    colliding_task_title: str
    colliding_start: datetime
    colliding_end: datetime


@dataclass
class NoFitResult:
    item_title: str
    duration_minutes: Optional[int]
    reason: str


def _to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz).replace(tzinfo=None)


def _occurs_on(recurrence_rule: str, anchor_start_local: datetime, target_date: date_) -> bool:
    rule = rrulestr(recurrence_rule, dtstart=anchor_start_local)
    day_start = datetime.combine(target_date, time_.min)
    day_end = datetime.combine(target_date, time_.max)
    return len(rule.between(day_start, day_end, inc=True)) > 0


def get_effective_schedule(db: Session, user_id: int, target_date: date_) -> list[BusyBlock]:
    """Part 1: the day's actual busy times.

    1. Default fixed-event template for this weekday (RRULE-expanded).
    2. Minus/shifted by any fixed_event_overrides row for this exact date.
    3. Plus tasks already placed (have a start_time) for this date.
    """
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    tz = ZoneInfo(user.timezone)

    blocks: list[BusyBlock] = []

    fixed_events = (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.type == "fixed_event", Item.status != "cancelled")
        .all()
    )

    for event in fixed_events:
        anchor_start_local = _to_local(event.start_time, tz)
        anchor_end_local = _to_local(event.end_time, tz)
        if not _occurs_on(event.recurrence_rule, anchor_start_local, target_date):
            continue

        occ_start = datetime.combine(target_date, anchor_start_local.time())
        occ_end = datetime.combine(target_date, anchor_end_local.time())

        override = (
            db.query(FixedEventOverride)
            .filter(FixedEventOverride.item_id == event.id, FixedEventOverride.override_date == target_date)
            .first()
        )

        if override is None:
            blocks.append(BusyBlock(occ_start, occ_end, event.title, "fixed_event", event.id))
        elif override.skip:
            continue  # temporarily removed for this date only
        else:
            for seg_start, seg_end in (
                (override.segment_1_start, override.segment_1_end),
                (override.segment_2_start, override.segment_2_end),
            ):
                if seg_start is not None and seg_end is not None:
                    blocks.append(
                        BusyBlock(
                            _to_local(seg_start, tz), _to_local(seg_end, tz), event.title, "fixed_event", event.id
                        )
                    )

    # Tasks that already have a start_time are "already placed" - both
    # auto-fit-scheduled tasks and confirmed explicit-time items end up
    # here (both are stored as type='task', just filled in differently).
    tasks = (
        db.query(Item)
        .filter(
            Item.user_id == user_id,
            Item.type == "task",
            Item.status != "cancelled",
            Item.start_time.isnot(None),
        )
        .all()
    )

    for task in tasks:
        local_start = _to_local(task.start_time, tz)
        if local_start.date() == target_date:
            blocks.append(BusyBlock(local_start, _to_local(task.end_time, tz), task.title, "task", task.id))

    blocks.sort(key=lambda b: b.start)
    return blocks


def find_gaps(
    busy_blocks: list[BusyBlock], wake_dt: datetime, sleep_dt: datetime
) -> list[tuple[datetime, datetime]]:
    """Free ranges between wake and sleep - standard interval-scheduling
    gap-finding, not just gaps between fixed events."""
    gaps: list[tuple[datetime, datetime]] = []
    cursor = wake_dt

    for block in sorted(busy_blocks, key=lambda b: b.start):
        if block.end <= cursor or block.start >= sleep_dt:
            continue
        if block.start > cursor:
            gaps.append((cursor, min(block.start, sleep_dt)))
        cursor = max(cursor, block.end)

    if cursor < sleep_dt:
        gaps.append((cursor, sleep_dt))

    return [(s, e) for s, e in gaps if s < e]


def filter_gaps_by_preference(
    gaps: list[tuple[datetime, datetime]],
    preference: Optional[str],
    reference_date: date_,
    sleep_dt: datetime,
) -> list[tuple[datetime, datetime]]:
    if preference is None:
        return gaps

    window_start_time, window_end_time = TIME_OF_DAY_WINDOWS[preference]
    window_start = datetime.combine(reference_date, window_start_time)
    window_end = sleep_dt if window_end_time is None else datetime.combine(reference_date, window_end_time)

    clipped = ((max(s, window_start), min(e, window_end)) for s, e in gaps)
    return [(s, e) for s, e in clipped if s < e]


@dataclass
class UntimedTaskRequest:
    title: str
    duration_minutes: int
    time_of_day_preference: Optional[str] = None
    urgent: bool = False
    important: bool = False


def place_untimed_tasks(
    busy_blocks: list[BusyBlock],
    wake_dt: datetime,
    sleep_dt: datetime,
    requests: list[UntimedTaskRequest],
) -> tuple[list[Placement], list[NoFitResult], list[BusyBlock]]:
    """Part 2: places a batch of untimed tasks from one message against an
    already-computed effective schedule. Urgent items get first pick;
    otherwise placed in the order given (stable sort preserves message
    order within each urgency group). Each placement is folded into the
    working block list before the next task is placed, so a batch never
    double-books itself. No buffer time between items."""
    ordered = sorted(requests, key=lambda r: not r.urgent)

    placements: list[Placement] = []
    no_fits: list[NoFitResult] = []
    blocks = list(busy_blocks)
    reference_date = wake_dt.date()

    for req in ordered:
        gaps = find_gaps(blocks, wake_dt, sleep_dt)
        gaps = filter_gaps_by_preference(gaps, req.time_of_day_preference, reference_date, sleep_dt)
        duration = timedelta(minutes=req.duration_minutes)

        fit = next((g for g in gaps if (g[1] - g[0]) >= duration), None)
        if fit is None:
            reason = "No gap large enough today"
            if req.time_of_day_preference:
                reason += f" in the {req.time_of_day_preference}"
            no_fits.append(NoFitResult(req.title, req.duration_minutes, reason))
            continue

        start = fit[0]
        end = start + duration
        placements.append(Placement(req.title, start, end, important=req.important))
        blocks.append(BusyBlock(start, end, req.title, "task"))
        blocks.sort(key=lambda b: b.start)

    return placements, no_fits, blocks


def _compute_split_segments(
    block_start: datetime, block_end: datetime, collide_start: datetime, collide_end: datetime
) -> list[tuple[datetime, datetime]]:
    """The sub-ranges of a fixed block that remain busy once a colliding
    explicit-time item is carved out. Empty list = the collider fully
    consumes the block for this date."""
    segments = []
    if collide_start > block_start:
        segments.append((block_start, collide_start))
    if collide_end < block_end:
        segments.append((collide_end, block_end))
    return segments


def place_explicit_time_item(
    target_date: date_,
    title: str,
    start: datetime,
    end: datetime,
    busy_blocks: list[BusyBlock],
    important: bool = False,
) -> Placement | CollisionResult:
    """Part 2: places an item with an explicit clock time.

    - Collides with a fixed_event -> auto-split that block for this date
      only, no need to ask (returned as fixed_event_splits on the Placement
      for the orchestrator to persist at confirm-time).
    - Collides with a task -> NOT auto-resolved; returns a CollisionResult
      for the orchestration layer to ask the user which one should move.
    """
    splits: list[FixedEventSplit] = []

    for block in busy_blocks:
        if not (start < block.end and end > block.start):
            continue

        if block.source == "task":
            return CollisionResult(
                new_item_title=title,
                new_item_start=start,
                new_item_end=end,
                colliding_task_id=block.item_id,
                colliding_task_title=block.title,
                colliding_start=block.start,
                colliding_end=block.end,
            )

        segments = _compute_split_segments(block.start, block.end, start, end)
        splits.append(FixedEventSplit(block.item_id, block.title, target_date, segments))

    return Placement(title, start, end, fixed_event_splits=splits, important=important)
