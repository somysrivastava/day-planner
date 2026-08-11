from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime
from datetime import time as time_
from datetime import timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.models import FixedEventOverride, Item, User
from app.services import calendar, scheduler_jobs
from app.services.parser import ExplicitTimeItem, ParsedMessage, ReminderItem, RecurringTaskItem, TaskItem
from app.services.scheduler import (
    BusyBlock,
    CollisionResult,
    NoFitResult,
    Placement,
    UntimedTaskRequest,
    filter_gaps_by_preference,
    find_gaps,
    get_effective_schedule,
    place_explicit_time_item,
    place_untimed_tasks,
)

WEEKDAY_CODE_TO_INT = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
DEFAULT_RECURRING_DURATION_MINUTES = 30  # fallback only; duration should normally already be resolved by Step 2


def _next_occurrence_date(from_date: date_, days: list[str]) -> date_:
    target_weekdays = {WEEKDAY_CODE_TO_INT[d] for d in days}
    for offset in range(7):
        candidate = from_date + timedelta(days=offset)
        if candidate.weekday() in target_weekdays:
            return candidate
    raise ValueError(f"No date in the next 7 days matches days={days}")


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _last_day_of_month_date(year: int, month: int) -> date_:
    return date_(year, month, monthrange(year, month)[1])


def _resolve_last_day_of_month_item(item: ReminderItem, reference_date: date_) -> ReminderItem:
    """Corrects `date` deterministically for a last_day_of_month reminder,
    regardless of what the parser guessed - the LLM only needs to detect
    the *intent* ('last day of every month'), not do exact calendar math
    (leap years, 30- vs 31-day months). reference_date's own month always
    qualifies as 'the next upcoming last day' - the last day of a month is
    never earlier than any other day within that same month."""
    resolved = _last_day_of_month_date(reference_date.year, reference_date.month)
    return item.model_copy(update={"date": resolved.isoformat()})


def _next_valid_day_of_month(anchor: date_, new_day: int) -> date_:
    """Same month/year as `anchor` if it actually has `new_day` days,
    otherwise rolls forward one month at a time until it finds one that
    does (e.g. picking 30 while `anchor` is in February)."""
    year, month = anchor.year, anchor.month
    while True:
        if new_day <= monthrange(year, month)[1]:
            return date_(year, month, new_day)
        month += 1
        if month > 12:
            month, year = 1, year + 1


@dataclass
class RecurringGroup:
    title: str
    days: list[str]
    start: datetime  # naive local, on the group's anchor date
    end: datetime


@dataclass
class MonthLengthWarning:
    """A recurring reminder anchored on the 29th/30th/31st - per
    scheduler-algorithm.md's 'Month-length edge case', this needs an
    explicit confirm-time flag ("won't fire in shorter months like
    February - keep as-is, or adjust?") rather than silently confirming.
    Holds the original parsed item, not yet added to plan.reminders."""

    reminder: ReminderItem


@dataclass
class Plan:
    user_id: int
    target_date: date_
    task_placements: list[Placement] = field(default_factory=list)
    explicit_placements: list[Placement] = field(default_factory=list)
    recurring_groups: list[RecurringGroup] = field(default_factory=list)
    reminders: list[ReminderItem] = field(default_factory=list)
    skip_notes: list[str] = field(default_factory=list)
    pending_collisions: list[CollisionResult] = field(default_factory=list)
    pending_no_fits: list[NoFitResult] = field(default_factory=list)
    # day (and everything else) known, just missing a duration - the plain
    # "how long do you think that'll take?" ask from Step 2 of the spec.
    pending_duration_questions: list[TaskItem] = field(default_factory=list)
    # bare-phrase, no day/duration at all - the fuller Today/Tomorrow/Choose
    # date -> Set a time quick-option flow from the "Vague/keyword-only" spec.
    pending_clarifications: list[TaskItem] = field(default_factory=list)
    # A recurring reminder anchored on the 29th/30th/31st - see
    # MonthLengthWarning. Resolved via resolve_month_length_warning().
    pending_month_length_warnings: list[MonthLengthWarning] = field(default_factory=list)
    # Items resolved to a different date than this plan's target_date - a
    # single build_plan call only schedules one date. Carries the actual
    # item (not just a note) so a caller can act on it - route_message()
    # below does this automatically by building one Plan per distinct
    # date; a caller invoking build_plan directly with a mixed-date
    # message sees these listed in summary_text() instead of losing them.
    out_of_scope_items: list[tuple[date_, TaskItem | ExplicitTimeItem]] = field(default_factory=list)

    @property
    def has_pending_issues(self) -> bool:
        return bool(
            self.pending_collisions
            or self.pending_no_fits
            or self.pending_duration_questions
            or self.pending_clarifications
            or self.pending_month_length_warnings
        )

    def summary_text(self) -> str:
        lines = [f"Here's the plan for {self.target_date.isoformat()}:"]
        for p in sorted(self.explicit_placements + self.task_placements, key=lambda p: p.start):
            lines.append(f"  - {p.title} at {p.start.strftime('%I:%M%p').lstrip('0')}")
        for g in self.recurring_groups:
            days = "/".join(g.days)
            lines.append(f"  - {g.title} at {g.start.strftime('%I:%M%p').lstrip('0')} ({days}, going forward)")
        for r in self.reminders:
            if r.recurring:
                suffix = " (last day of every month, going forward)" if r.last_day_of_month else " (every month, going forward)"
            else:
                suffix = ""
            lines.append(f"  - Reminder: {r.title} on {r.date}{suffix}")
        for note in self.skip_notes:
            lines.append(f"  - {note}")
        if self.pending_duration_questions:
            for t in self.pending_duration_questions:
                lines.append(f"  (need duration: how long do you think '{t.title}' will take?)")
        if self.pending_clarifications:
            for t in self.pending_clarifications:
                lines.append(f"  (need when: '{t.title}' - today, tomorrow, or choose a date?)")
        if self.pending_month_length_warnings:
            for w in self.pending_month_length_warnings:
                day = date_.fromisoformat(w.reminder.date).day
                lines.append(
                    f"  (heads up: '{w.reminder.title}' is set for the {_ordinal(day)} - won't fire in "
                    f"shorter months like February. Keep it as-is, or adjust it?)"
                )
        if self.pending_collisions:
            lines.append(f"  ({len(self.pending_collisions)} collision(s) still need your input)")
        if self.pending_no_fits:
            lines.append(f"  ({len(self.pending_no_fits)} item(s) with no fit found)")
        if self.out_of_scope_items:
            for d, item in self.out_of_scope_items:
                lines.append(f"  (not scheduled here - '{item.title}' is dated {d}, needs its own plan for that date)")
        if not self.has_pending_issues:
            lines.append("Sound good?")
        return "\n".join(lines)


def _skip_notes_for_date(db: Session, user_id: int, target_date: date_) -> list[str]:
    rows = (
        db.query(FixedEventOverride, Item)
        .join(Item, Item.id == FixedEventOverride.item_id)
        .filter(Item.user_id == user_id, FixedEventOverride.override_date == target_date, FixedEventOverride.skip.is_(True))
        .all()
    )
    return [f"{item.title}'s off for today and back tomorrow" for _override, item in rows]


def skip_fixed_event(db: Session, user_id: int, target_date: date_, title: str) -> None:
    """Simulates the effect of a 'skip gym today' message. Day 3's parser
    doesn't classify override intents (only reminder/task/recurring_task/
    explicit_time_item) - that's a 5th intent left for a future day. This
    lets us test the override mechanism and the worked example without
    silently faking a parser capability that doesn't exist yet."""
    event = db.query(Item).filter(Item.user_id == user_id, Item.type == "fixed_event", Item.title == title).first()
    if event is None:
        raise ValueError(f"No fixed_event titled {title!r} for user {user_id}")

    existing = (
        db.query(FixedEventOverride)
        .filter(FixedEventOverride.item_id == event.id, FixedEventOverride.override_date == target_date)
        .first()
    )
    if existing:
        existing.skip = True
        existing.segment_1_start = existing.segment_1_end = None
        existing.segment_2_start = existing.segment_2_end = None
    else:
        db.add(FixedEventOverride(item_id=event.id, override_date=target_date, skip=True))
    db.commit()


def _promote_recurring_task(
    db: Session,
    user_id: int,
    item: RecurringTaskItem,
    target_date: date_,
    blocks: list[BusyBlock],
    wake_time: time_,
    sleep_time: time_,
) -> tuple[list[RecurringGroup], list[NoFitResult]]:
    groups: list[RecurringGroup] = []
    no_fits: list[NoFitResult] = []
    duration = item.duration_minutes or DEFAULT_RECURRING_DURATION_MINUTES

    # Days with an explicit time need no gap search - group identical times together.
    by_time: dict[str, list[str]] = defaultdict(list)
    for t in item.times:
        if t.time is not None:
            by_time[t.time].append(t.day)
    for time_str, days in by_time.items():
        hh, mm = map(int, time_str.split(":"))
        anchor_date = _next_occurrence_date(target_date, days)
        start = datetime.combine(anchor_date, time_(hh, mm))
        groups.append(RecurringGroup(item.title, days, start, start + timedelta(minutes=duration)))

    # Days needing auto-fit: search against the next actual occurrence of
    # those specific days, not blindly against target_date.
    auto_days = [t.day for t in item.times if t.time is None]
    if auto_days:
        rep_date = _next_occurrence_date(target_date, auto_days)
        rep_blocks = blocks if rep_date == target_date else get_effective_schedule(db, user_id, rep_date)
        wake_dt = datetime.combine(rep_date, wake_time)
        sleep_dt = datetime.combine(rep_date, sleep_time)
        gaps = find_gaps(rep_blocks, wake_dt, sleep_dt)
        gaps = filter_gaps_by_preference(gaps, item.time_of_day_preference, rep_date, sleep_dt)
        dur = timedelta(minutes=duration)
        fit = next((g for g in gaps if (g[1] - g[0]) >= dur), None)
        if fit is None:
            no_fits.append(NoFitResult(item.title, duration, "No gap large enough to place this recurring task"))
        else:
            anchor_date = _next_occurrence_date(target_date, auto_days)
            start = datetime.combine(anchor_date, fit[0].time())
            groups.append(RecurringGroup(item.title, auto_days, start, start + timedelta(minutes=duration)))

    return groups, no_fits


_DEDUPE_TYPE_PRIORITY = {"explicit_time_item": 0, "task": 1}  # lower wins - explicit time is more information


def _dedupe_parsed_items(items: list) -> list:
    """Narrow safeguard against a real, still-present parser non-
    determinism (see PROGRESS.md's Day 7 notes): occasionally, one thing
    the user mentioned comes back as two items in the same message - an
    explicit_time_item plus a redundant task, or two explicit_time_items -
    which would otherwise write two real bookings for one thing. Only
    collapses when BOTH items are task/explicit_time_item AND share an
    exact normalized title AND an identical *stated* duration_minutes
    (never when duration is still None - that's a legitimately unresolved
    task, not a duplicate, and collapsing it here would risk silently
    dropping a real second task before its own duration question is even
    asked). This deliberately doesn't touch reminder/recurring_task, or
    same-title items with differing durations - narrow enough that two
    genuinely different same-titled things in one message essentially
    never accidentally collide, at the cost of not catching every possible
    duplication shape the parser might produce."""
    kept: list = []
    seen: dict[tuple[str, int], int] = {}
    for item in items:
        item_type = getattr(item, "type", None)
        duration = getattr(item, "duration_minutes", None)
        if item_type not in _DEDUPE_TYPE_PRIORITY or duration is None:
            kept.append(item)
            continue
        key = (item.title.strip().lower(), duration)
        if key in seen:
            idx = seen[key]
            if _DEDUPE_TYPE_PRIORITY[item_type] < _DEDUPE_TYPE_PRIORITY[kept[idx].type]:
                kept[idx] = item
            continue
        seen[key] = len(kept)
        kept.append(item)
    return kept


def build_plan(db: Session, user_id: int, target_date: date_, parsed: ParsedMessage) -> Plan:
    """Part 3: routes each parsed item through the scheduler and assembles
    one plan for the whole message - explicit-time items first (they're
    fixed points), then untimed tasks, then recurring-task promotion."""
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

    blocks = get_effective_schedule(db, user_id, target_date)
    plan = Plan(user_id=user_id, target_date=target_date, skip_notes=_skip_notes_for_date(db, user_id, target_date))
    deduped_items = _dedupe_parsed_items(parsed.items)

    for item in deduped_items:
        if isinstance(item, ExplicitTimeItem):
            if date_.fromisoformat(item.date) != target_date:
                plan.out_of_scope_items.append((date_.fromisoformat(item.date), item))
                continue
            hh, mm = map(int, item.start_time.split(":"))
            start = datetime.combine(target_date, time_(hh, mm))
            duration = item.duration_minutes or DEFAULT_RECURRING_DURATION_MINUTES
            end = start + timedelta(minutes=duration)
            result = place_explicit_time_item(target_date, item.title, start, end, blocks, important=item.important)
            if isinstance(result, CollisionResult):
                plan.pending_collisions.append(result)
            else:
                plan.explicit_placements.append(result)
                blocks.append(BusyBlock(result.start, result.end, result.title, "task"))
                blocks.sort(key=lambda b: b.start)

    task_requests = []
    for t in deduped_items:
        if not isinstance(t, TaskItem):
            continue
        if t.day is not None and date_.fromisoformat(t.day) != target_date:
            plan.out_of_scope_items.append((date_.fromisoformat(t.day), t))
        elif t.needs_clarification:
            plan.pending_clarifications.append(t)
        elif t.duration_minutes is None:
            plan.pending_duration_questions.append(t)
        else:
            task_requests.append(
                UntimedTaskRequest(t.title, t.duration_minutes, t.time_of_day_preference, t.urgent, t.important)
            )

    if task_requests:
        wake_dt = _effective_start_bound(user, target_date, datetime.combine(target_date, user.wake_time))
        sleep_dt = datetime.combine(target_date, user.sleep_time)
        placements, no_fits, blocks = place_untimed_tasks(blocks, wake_dt, sleep_dt, task_requests)
        plan.task_placements.extend(placements)
        plan.pending_no_fits.extend(no_fits)

    for item in deduped_items:
        if isinstance(item, RecurringTaskItem):
            groups, no_fits = _promote_recurring_task(
                db, user_id, item, target_date, blocks, user.wake_time, user.sleep_time
            )
            plan.recurring_groups.extend(groups)
            plan.pending_no_fits.extend(no_fits)
        elif isinstance(item, ReminderItem):
            if item.last_day_of_month:
                item = _resolve_last_day_of_month_item(item, target_date)
            if item.recurring and not item.last_day_of_month and date_.fromisoformat(item.date).day >= 29:
                plan.pending_month_length_warnings.append(MonthLengthWarning(item))
            else:
                plan.reminders.append(item)

    return plan


def route_message(
    db: Session, user_id: int, reference_date: date_, parsed: ParsedMessage
) -> dict[date_, "Plan"]:
    """Recommended entry point for a full message. Partitions items by
    their resolved date *before* calling build_plan, so a message mixing
    dates ("gym today, submit the report Friday") produces one Plan per
    date instead of one Plan silently missing the Friday item. Recurring
    tasks and reminders always anchor to reference_date (the day the
    message was sent), since that's what "starting today" / "in 3 days"
    are relative to."""
    items_by_date: dict[date_, list] = defaultdict(list)

    for item in parsed.items:
        if isinstance(item, TaskItem):
            d = date_.fromisoformat(item.day) if item.day else reference_date
            items_by_date[d].append(item)
        elif isinstance(item, ExplicitTimeItem):
            items_by_date[date_.fromisoformat(item.date)].append(item)
        else:
            items_by_date[reference_date].append(item)

    return {d: build_plan(db, user_id, d, ParsedMessage(items=items)) for d, items in sorted(items_by_date.items())}


def resolve_collision(
    db: Session, plan: Plan, collision: CollisionResult, choice: Literal["move_existing", "keep_new_elsewhere"]
) -> None:
    """Neither side auto-resolves on its own (per spec) - the caller
    (WhatsApp reply / CLI prompt) supplies the user's choice."""
    plan.pending_collisions.remove(collision)
    user = db.get(User, plan.user_id)
    wake_dt = _effective_start_bound(user, plan.target_date, datetime.combine(plan.target_date, user.wake_time))
    sleep_dt = datetime.combine(plan.target_date, user.sleep_time)
    current_blocks = _current_plan_blocks(db, plan)

    if choice == "move_existing":
        # The new item keeps its stated time; the old task gets re-placed elsewhere.
        plan.explicit_placements.append(
            Placement(collision.new_item_title, collision.new_item_start, collision.new_item_end)
        )
        remaining = [b for b in current_blocks if not (b.start == collision.colliding_start and b.title == collision.colliding_task_title)]
        remaining.append(BusyBlock(collision.new_item_start, collision.new_item_end, collision.new_item_title, "task"))
        duration = int((collision.colliding_end - collision.colliding_start).total_seconds() // 60)
        placements, no_fits, _ = place_untimed_tasks(
            remaining, wake_dt, sleep_dt, [UntimedTaskRequest(collision.colliding_task_title, duration)]
        )
        plan.task_placements.extend(placements)
        plan.pending_no_fits.extend(no_fits)
    else:
        # The existing task stays; the new item needs a different time instead.
        duration = int((collision.new_item_end - collision.new_item_start).total_seconds() // 60)
        placements, no_fits, _ = place_untimed_tasks(
            current_blocks, wake_dt, sleep_dt, [UntimedTaskRequest(collision.new_item_title, duration)]
        )
        plan.task_placements.extend(placements)
        plan.pending_no_fits.extend(no_fits)


def resolve_no_fit(
    db: Session, plan: Plan, no_fit: NoFitResult, choice: Literal["shift_to_tomorrow", "force_today"]
) -> Optional[str]:
    """Returns a note for shift_to_tomorrow (the caller should build a
    separate Plan for target_date + 1 day); returns None for force_today
    (placed directly into this plan, sleep-hours authorized)."""
    plan.pending_no_fits.remove(no_fit)

    if choice == "shift_to_tomorrow":
        return f"{no_fit.item_title} shifted to {plan.target_date + timedelta(days=1)}"

    user = db.get(User, plan.user_id)
    current_blocks = _current_plan_blocks(db, plan)
    # Forced: search the full day, not just wake-sleep bounds - the user is
    # explicitly authorizing sleep-hour placement. Still never into the
    # past, though - "force it into today" can't mean before "now".
    day_start = _effective_start_bound(user, plan.target_date, datetime.combine(plan.target_date, time_.min))
    day_end = datetime.combine(plan.target_date, time_.max)
    placements, still_no_fit, _ = place_untimed_tasks(
        current_blocks, day_start, day_end, [UntimedTaskRequest(no_fit.item_title, no_fit.duration_minutes or 30)]
    )
    plan.task_placements.extend(placements)
    plan.pending_no_fits.extend(still_no_fit)  # only non-empty if it genuinely can't fit even forced
    return None


def resolve_duration_question(db: Session, plan: Plan, task: TaskItem, duration_minutes: int) -> None:
    """Answers the plain 'how long do you think that'll take?' ask (Step 2
    of the spec) - day and everything else was already known."""
    plan.pending_duration_questions.remove(task)
    user = db.get(User, plan.user_id)
    wake_dt = _effective_start_bound(user, plan.target_date, datetime.combine(plan.target_date, user.wake_time))
    sleep_dt = datetime.combine(plan.target_date, user.sleep_time)
    blocks = _current_plan_blocks(db, plan)
    placements, no_fits, _ = place_untimed_tasks(
        blocks, wake_dt, sleep_dt,
        [UntimedTaskRequest(task.title, duration_minutes, task.time_of_day_preference, task.urgent, task.important)],
    )
    plan.task_placements.extend(placements)
    plan.pending_no_fits.extend(no_fits)


def resolve_clarification(
    db: Session,
    plan: Plan,
    task: TaskItem,
    day: date_,
    duration_minutes: int,
    time_of_day_preference: Optional[str] = None,
) -> Optional[str]:
    """Answers the fuller Today/Tomorrow/Choose-date -> Set-a-time quick-
    option flow for a bare-phrase task ("alter clothes")."""
    plan.pending_clarifications.remove(task)
    if day != plan.target_date:
        return f"'{task.title}' resolved to {day} - build a separate plan for that date"
    user = db.get(User, plan.user_id)
    wake_dt = _effective_start_bound(user, plan.target_date, datetime.combine(plan.target_date, user.wake_time))
    sleep_dt = datetime.combine(plan.target_date, user.sleep_time)
    blocks = _current_plan_blocks(db, plan)
    placements, no_fits, _ = place_untimed_tasks(
        blocks, wake_dt, sleep_dt,
        [UntimedTaskRequest(task.title, duration_minutes, time_of_day_preference, task.urgent, task.important)],
    )
    plan.task_placements.extend(placements)
    plan.pending_no_fits.extend(no_fits)
    return None


def resolve_month_length_warning(
    plan: Plan,
    warning: MonthLengthWarning,
    choice: Literal["keep_as_is", "different_day", "last_day_of_month"],
    new_day: Optional[int] = None,
) -> None:
    """Answers the 29th/30th/31st recurring-reminder warning (see
    MonthLengthWarning). No DB access needed - like every other resolver,
    this only edits the in-memory Plan; confirm_plan is still the only
    thing that writes to the DB.

    - keep_as_is: confirms the reminder exactly as parsed, knowingly
      skipping shorter months.
    - different_day: moves it to a new fixed day-of-month (`new_day`,
      required), rolling forward to the next month that actually has that
      day if the current one doesn't (e.g. picking 30 while resolving in
      February).
    - last_day_of_month: switches to the dedicated last-day-of-month
      recurrence (BYMONTHDAY=-1 at confirm time) - a real, distinct
      recurrence option per the spec, not a fallback.
    """
    plan.pending_month_length_warnings.remove(warning)
    reminder = warning.reminder

    if choice == "keep_as_is":
        plan.reminders.append(reminder)
    elif choice == "different_day":
        if new_day is None or not (1 <= new_day <= 31):
            raise ValueError("new_day must be an int from 1 to 31")
        anchor = date_.fromisoformat(reminder.date)
        new_date = _next_valid_day_of_month(anchor, new_day)
        plan.reminders.append(reminder.model_copy(update={"date": new_date.isoformat()}))
    elif choice == "last_day_of_month":
        anchor = date_.fromisoformat(reminder.date)
        resolved = _last_day_of_month_date(anchor.year, anchor.month)
        plan.reminders.append(reminder.model_copy(update={"date": resolved.isoformat(), "last_day_of_month": True}))
    else:
        raise ValueError(f"Unknown choice {choice!r}")


def remove_item(plan: Plan, title: str) -> bool:
    """The 'no -> what would you like changed?' -> 'drop X' path. Nothing
    is persisted until confirm_plan runs, so this just edits the
    in-memory Plan - no DB/calendar cleanup needed. Returns True if
    something was actually removed."""
    for collection in (plan.explicit_placements, plan.task_placements):
        for p in list(collection):
            if p.title == title:
                collection.remove(p)
                return True
    for g in list(plan.recurring_groups):
        if g.title == title:
            plan.recurring_groups.remove(g)
            return True
    for r in list(plan.reminders):
        if r.title == title:
            plan.reminders.remove(r)
            return True
    return False


def _effective_start_bound(user: User, target_date: date_, floor_dt: datetime) -> datetime:
    """Raises floor_dt (normally wake_time, or midnight for a forced no-fit
    search) to real 'now' when target_date is today in the user's own
    timezone and now has already passed floor_dt. Without this, a message
    processed mid-day/evening with no explicit time preference gets
    auto-fit-placed at the day's *earliest* gap regardless of when it's
    actually being handled - e.g. a 9pm text with no time stated getting
    placed at 4:30am, hours already in the past. Found via the Day 7
    full-loop integration test (every earlier test used future seed dates,
    where 'today' was always still ahead of real 'now' at test time, so
    this was structurally invisible before). Same underlying fix as Day
    5's reschedule_confirmed_item min_start, generalized to every call site
    that places something for potentially-today rather than just that one."""
    tz = ZoneInfo(user.timezone)
    now_local = datetime.now(tz).replace(tzinfo=None)
    if target_date == now_local.date() and now_local > floor_dt:
        return now_local
    return floor_dt


def _place_single_task(
    user: User,
    target_date: date_,
    blocks: list[BusyBlock],
    title: str,
    duration_minutes: int,
    time_of_day_preference: Optional[str] = None,
    urgent: bool = False,
    important: bool = False,
    min_start: Optional[datetime] = None,
) -> Placement | NoFitResult:
    """Shared core behind both reschedule_item (pre-confirm, Plan-level)
    and reschedule_confirmed_item (post-confirm, DB-level) - the actual
    gap-search is identical either way, only what happens with the result
    differs.

    min_start raises the lower search bound above wake_time when needed -
    critically, for a same-day reschedule happening in real time (e.g. the
    completion check-in's "no" answer), gap-finding is otherwise blind to
    the actual current time and can happily place the new slot earlier
    today than "now", which is meaningless (you can't do a task in the
    past) and silently produces a nudge/check-in that never gets
    scheduled, since their own run_date-in-the-past check correctly
    refuses. reschedule_confirmed_item passes real "now" here for today;
    every other caller leaves this None since they only ever deal with
    already-resolved future dates from the parser, where "now" is
    irrelevant."""
    wake_dt = datetime.combine(target_date, user.wake_time)
    if min_start is not None and min_start > wake_dt:
        wake_dt = min_start
    sleep_dt = datetime.combine(target_date, user.sleep_time)
    placements, no_fits, _ = place_untimed_tasks(
        blocks, wake_dt, sleep_dt, [UntimedTaskRequest(title, duration_minutes, time_of_day_preference, urgent, important)]
    )
    return placements[0] if placements else no_fits[0]


def reschedule_item(
    db: Session,
    plan: Plan,
    title: str,
    new_duration_minutes: Optional[int] = None,
    new_time_of_day_preference: Optional[str] = None,
) -> Optional[NoFitResult]:
    """The 'no -> what would you like changed?' -> 'move X' (or change its
    duration/time-of-day) path. Only applies to already-placed untimed
    tasks - an explicit-time item's time came from the user directly, so
    'change its time' means a new message with a new stated time, not
    auto-fit re-placement. Returns a NoFitResult if the new constraints
    genuinely don't fit anywhere (item is dropped from the plan in that
    case - same as any other no-fit).

    Nothing is persisted until confirm_plan runs, so there is never a live
    nudge/check-in job to cancel here - those only get scheduled once an
    item has a real DB row and start_time (see confirm_plan). The
    equivalent post-confirm operation, which does need to cancel/
    reschedule real jobs, is reschedule_confirmed_item below."""
    existing = next((p for p in plan.task_placements if p.title == title), None)
    if existing is None:
        raise ValueError(f"'{title}' isn't an untimed task placement in this plan")

    plan.task_placements.remove(existing)
    duration = new_duration_minutes or int((existing.end - existing.start).total_seconds() // 60)
    user = db.get(User, plan.user_id)
    blocks = _current_plan_blocks(db, plan)  # existing was already removed above, so its old slot is free
    # Passing this unconditionally is harmless when it equals wake_time
    # exactly - _place_single_task only raises the floor if min_start is
    # later than wake_dt.
    min_start = _effective_start_bound(user, plan.target_date, datetime.combine(plan.target_date, user.wake_time))

    result = _place_single_task(
        user, plan.target_date, blocks, title, duration, new_time_of_day_preference,
        important=existing.important, min_start=min_start,
    )
    if isinstance(result, NoFitResult):
        plan.pending_no_fits.append(result)
        return result
    plan.task_placements.append(result)
    return None


def reschedule_confirmed_item(
    db: Session, item_id: int, new_time_of_day_preference: Optional[str] = None, target_date: Optional[date_] = None
) -> Optional[NoFitResult]:
    """Post-confirm reschedule - used by both the completion check-in's
    'no, didn't get to it' answer (scheduler_jobs.answer_checkin) and the
    Day 7 evening check-in's 'tomorrow'/'choose a date' deferral
    (scheduler_jobs.answer_evening_checkin), reusing the same
    _place_single_task core as reschedule_item rather than a second
    placement path. This is the code path that actually needs to cancel a
    live nudge/check-in job and schedule fresh ones for the new time.

    target_date defaults to the item's own current date (the completion
    check-in's use case - reschedule later the same day or the search
    naturally spills to whatever's passed in). The evening check-in passes
    an explicit future date instead, since by definition it's deferring an
    already-past-today item to tomorrow or a chosen date, not re-searching
    today (which has already ended for that item's purposes)."""
    item = db.get(Item, item_id)
    if item is None:
        raise ValueError(f"Item {item_id} not found")
    if item.type != "task":
        raise ValueError(f"Item {item_id} is a {item.type!r}, not a reschedulable task")

    user = db.get(User, item.user_id)
    tz = ZoneInfo(user.timezone)
    if target_date is None:
        target_date = item.start_time.astimezone(tz).date()
    duration_minutes = int((item.end_time - item.start_time).total_seconds() // 60)

    scheduler_jobs.cancel_job(item.nudge_job_id)
    scheduler_jobs.cancel_job(item.checkin_job_id)
    item.nudge_job_id = None
    item.checkin_job_id = None

    min_start = _effective_start_bound(user, target_date, datetime.combine(target_date, user.wake_time))

    blocks = [b for b in get_effective_schedule(db, item.user_id, target_date) if b.item_id != item.id]
    result = _place_single_task(
        user, target_date, blocks, item.title, duration_minutes, new_time_of_day_preference,
        important=item.important, min_start=min_start,
    )

    if isinstance(result, NoFitResult):
        db.commit()
        return result

    item.start_time = result.start.replace(tzinfo=tz)
    item.end_time = result.end.replace(tzinfo=tz)

    if item.google_event_id:
        try:
            calendar.delete_event(db, item.user_id, item.google_event_id)
        except Exception as exc:
            print(f"  (calendar cleanup skipped: {exc})")

    item.google_event_id = _sync_to_calendar_best_effort(
        db,
        item.user_id,
        summary=item.title,
        description="Rescheduled via Day Planner",
        start_datetime=item.start_time.isoformat(),
        end_datetime=item.end_time.isoformat(),
        time_zone=user.timezone,
    )

    item.nudge_job_id = scheduler_jobs.schedule_nudge(
        item.id, result.start - timedelta(minutes=user.nudge_lead_minutes), user.timezone
    )
    if item.important:
        item.checkin_job_id = scheduler_jobs.schedule_checkin(item.id, result.end, user.timezone)

    db.commit()
    return None


def _current_plan_blocks(db: Session, plan: Plan) -> list[BusyBlock]:
    """Effective schedule plus everything this Plan has already decided -
    including recurring_groups, which build_plan may have promoted before
    a pending duration question/clarification/collision on an earlier item
    in the same message gets resolved in a later call."""
    blocks = get_effective_schedule(db, plan.user_id, plan.target_date)
    for p in plan.explicit_placements + plan.task_placements:
        blocks.append(BusyBlock(p.start, p.end, p.title, "task"))
    for g in plan.recurring_groups:
        if plan.target_date.weekday() in {WEEKDAY_CODE_TO_INT[d] for d in g.days}:
            blocks.append(BusyBlock(g.start, g.end, g.title, "fixed_event"))
    blocks.sort(key=lambda b: b.start)
    return blocks


def _sync_to_calendar_best_effort(db: Session, user_id: int, **kwargs) -> Optional[str]:
    """Calendar sync is 'if relevant' per the spec, not a hard requirement -
    a user with no Google Calendar connected (all 3 synthetic test users,
    or a real user who hasn't authorized yet) should still get their item
    saved to `items`. Returns the Google event id, or None if sync failed
    for any reason."""
    try:
        event = calendar.create_event(db, user_id, **kwargs)
        return event["id"]
    except Exception as exc:
        print(f"  (calendar sync skipped: {exc})")
        return None


def confirm_plan(db: Session, plan: Plan) -> list[Item]:
    """The only function in this module that writes to the DB or calls
    Google Calendar. Refuses to run while anything is still pending."""
    if plan.has_pending_issues:
        raise ValueError("Plan still has unresolved collisions or no-fits - resolve them before confirming")

    user = db.get(User, plan.user_id)
    tz = ZoneInfo(user.timezone)
    saved: list[Item] = []

    def to_aware(local_dt: datetime) -> datetime:
        return local_dt.replace(tzinfo=tz)

    for placement in plan.explicit_placements + plan.task_placements:
        row = Item(
            user_id=plan.user_id,
            type="task",
            title=placement.title,
            start_time=to_aware(placement.start),
            end_time=to_aware(placement.end),
            important=placement.important,
        )
        db.add(row)
        db.flush()  # get row.id before splitting overrides / calling Calendar

        for split in placement.fixed_event_splits:
            payload = dict(item_id=split.item_id, override_date=split.override_date)
            if not split.segments:
                payload.update(skip=True)
            else:
                payload["segment_1_start"], payload["segment_1_end"] = (
                    to_aware(split.segments[0][0]),
                    to_aware(split.segments[0][1]),
                )
                if len(split.segments) > 1:
                    payload["segment_2_start"], payload["segment_2_end"] = (
                        to_aware(split.segments[1][0]),
                        to_aware(split.segments[1][1]),
                    )
            existing = (
                db.query(FixedEventOverride)
                .filter(FixedEventOverride.item_id == split.item_id, FixedEventOverride.override_date == split.override_date)
                .first()
            )
            if existing:
                for k, v in payload.items():
                    setattr(existing, k, v)
            else:
                db.add(FixedEventOverride(**payload))

        row.google_event_id = _sync_to_calendar_best_effort(
            db,
            plan.user_id,
            summary=placement.title,
            description="Added via Day Planner",
            start_datetime=to_aware(placement.start).isoformat(),
            end_datetime=to_aware(placement.end).isoformat(),
            time_zone=user.timezone,
        )

        # Nudge applies to any scheduled task with a specific time, not just
        # important-flagged ones. Check-in is opt-in, important-only.
        row.nudge_job_id = scheduler_jobs.schedule_nudge(
            row.id, placement.start - timedelta(minutes=user.nudge_lead_minutes), user.timezone
        )
        if placement.important:
            row.checkin_job_id = scheduler_jobs.schedule_checkin(row.id, placement.end, user.timezone)

        saved.append(row)

    for group in plan.recurring_groups:
        rrule = f"FREQ=WEEKLY;BYDAY={','.join(group.days)}"
        row = Item(
            user_id=plan.user_id,
            type="fixed_event",
            title=group.title,
            start_time=to_aware(group.start),
            end_time=to_aware(group.end),
            recurrence_rule=rrule,
        )
        db.add(row)
        db.flush()

        row.google_event_id = _sync_to_calendar_best_effort(
            db,
            plan.user_id,
            summary=group.title,
            description="Recurring task added via Day Planner",
            start_datetime=to_aware(group.start).isoformat(),
            end_datetime=to_aware(group.end).isoformat(),
            time_zone=user.timezone,
            recurrence=[f"RRULE:{rrule}"],
        )

        # Nudges extend to newly-confirmed fixed events too (per spec:
        # "any scheduled task or fixed event with a specific time"), via a
        # recurring CronTrigger matching the group's days. Pre-existing
        # seeded fixed events (Gym/Office/etc, never created through
        # confirm_plan) deliberately don't get this - no natural hook.
        nudge_time = (group.start - timedelta(minutes=user.nudge_lead_minutes)).time()
        row.nudge_job_id = scheduler_jobs.schedule_recurring_nudge(
            row.id, group.days, nudge_time.hour, nudge_time.minute, user.timezone
        )
        # Recurring tasks/fixed events don't get a completion check-in -
        # "did you do it today" doesn't fit an ongoing habit the way it
        # fits a one-off task; important-flagging isn't offered on
        # RecurringTaskItem at the parser level either (see parser.py).

        saved.append(row)

    for reminder in plan.reminders:
        # Reminders have no time slot - start_time stores just the trigger
        # date (midnight local), reused rather than adding a new column.
        # Week 2 Day 1: recurring reminders reuse recurrence_rule too (same
        # column fixed_event uses) - safe because get_effective_schedule
        # only ever reads recurrence_rule for type='fixed_event' rows, so a
        # recurring reminder is structurally invisible to the auto-fit
        # scheduler, per the spec's "no interaction with the scheduler."
        anchor_date = date_.fromisoformat(reminder.date)
        if not reminder.recurring:
            recurrence_rule = None
        elif reminder.last_day_of_month:
            # BYMONTHDAY=-1 is dateutil.rrule's/RFC5545's standard negative-
            # offset form - "last day of the month," correctly 28/29/30/31
            # depending on the actual month, not a special case this code
            # has to compute per-occurrence itself.
            recurrence_rule = "FREQ=MONTHLY;BYMONTHDAY=-1"
        else:
            recurrence_rule = f"FREQ=MONTHLY;BYMONTHDAY={anchor_date.day}"
        row = Item(
            user_id=plan.user_id,
            type="reminder",
            title=reminder.title,
            start_time=to_aware(datetime.combine(anchor_date, time_.min)),
            recurrence_rule=recurrence_rule,
        )
        db.add(row)
        saved.append(row)

    db.commit()
    return saved
