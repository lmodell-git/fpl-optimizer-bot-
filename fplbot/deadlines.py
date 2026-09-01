"""Dynamic deadline logic.

The FPL deadline is 90 minutes before the first kickoff of a gameweek and is
published as `deadline_time` on each event in bootstrap-static. It lands on a
Friday *most* weeks but routinely shifts to Saturday 11:00, a Sunday, or a
midweek slot around Christmas and rearranged fixtures. Nothing here is
hardcoded to a weekday — everything is derived from the API each run.

Two questions this module answers:
  1. When is the next deadline?                       -> next_deadline()
  2. Should the bot act / notify on this run?         -> notification_state()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import fpl_api


def _parse(ts: str) -> datetime:
    """Parse an FPL ISO timestamp ('...Z') into an aware UTC datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class Deadline:
    event_id: int
    name: str
    deadline: datetime  # aware, UTC

    def hours_away(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.deadline - now).total_seconds() / 3600.0


def next_deadline(now: datetime | None = None) -> Deadline | None:
    """First event whose deadline is still in the future. None if the season is over."""
    now = now or datetime.now(timezone.utc)
    for ev in fpl_api.events():
        dl = _parse(ev["deadline_time"])
        if dl > now:
            return Deadline(event_id=ev["id"], name=ev["name"], deadline=dl)
    return None


@dataclass(frozen=True)
class NotificationState:
    should_notify: bool
    reason: str
    deadline: Deadline | None
    hours_away: float | None
    # How soon the workflow should check again (minutes). The GitHub workflow
    # reads this to decide whether to schedule a tighter re-run near a deadline.
    recheck_in_minutes: int


def notification_state(
    *,
    notify_window_hours: float = 26.0,
    tighten_window_hours: float = 3.0,
    now: datetime | None = None,
) -> NotificationState:
    """Decide whether this run is inside the pre-deadline notification window.

    - Outside `notify_window_hours` of the next deadline: nothing to do, check
      again tomorrow.
    - Inside it: send the recommendation email.
    - Inside `tighten_window_hours`: also tell the caller to re-run every
      15 minutes, so a slipped GitHub cron still fires before the deadline.
    """
    now = now or datetime.now(timezone.utc)
    dl = next_deadline(now)
    if dl is None:
        return NotificationState(False, "season over — no future deadline", None, None, 1440)

    h = dl.hours_away(now)
    if h <= 0:
        return NotificationState(
            False, f"deadline for {dl.name} already passed", dl, h, 60
        )
    if h <= tighten_window_hours:
        return NotificationState(
            True, f"{h:.1f}h to {dl.name} deadline — final window", dl, h, 15
        )
    if h <= notify_window_hours:
        return NotificationState(
            True, f"{h:.1f}h to {dl.name} deadline — notify window", dl, h, 180
        )
    # More than a day out. Re-check so that we land inside the window tomorrow.
    slack = h - notify_window_hours
    return NotificationState(
        False,
        f"{h:.1f}h to {dl.name} deadline — outside {notify_window_hours:.0f}h window",
        dl,
        h,
        max(60, min(1440, int(slack * 60))),
    )


def upcoming_deadlines(count: int = 8, now: datetime | None = None) -> list[Deadline]:
    """The next `count` deadlines — used by the multi-period solver's horizon."""
    now = now or datetime.now(timezone.utc)
    out: list[Deadline] = []
    for ev in fpl_api.events():
        dl = _parse(ev["deadline_time"])
        if dl > now:
            out.append(Deadline(ev["id"], ev["name"], dl))
        if len(out) >= count:
            break
    return out
