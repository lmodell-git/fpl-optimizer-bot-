"""Deadline-window logic tests (no network — monkeypatches fpl_api.events)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplbot import deadlines, fpl_api  # noqa: E402


def _events(next_deadline_in_hours: float):
    now = datetime.now(timezone.utc)
    dl = now + timedelta(hours=next_deadline_in_hours)
    return [
        {"id": 9, "name": "Gameweek 9", "deadline_time": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"id": 10, "name": "Gameweek 10", "deadline_time": dl.strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"id": 11, "name": "Gameweek 11", "deadline_time": (dl + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")},
    ]


def _patch(monkey_events):
    fpl_api.events.__wrapped__ if hasattr(fpl_api.events, "__wrapped__") else None
    deadlines.fpl_api.events = lambda: monkey_events  # type: ignore


def test_outside_window_does_not_notify():
    _patch(_events(50))
    ns = deadlines.notification_state(notify_window_hours=26)
    assert ns.should_notify is False
    assert ns.deadline.event_id == 10
    assert ns.recheck_in_minutes > 15


def test_inside_notify_window():
    _patch(_events(20))
    ns = deadlines.notification_state(notify_window_hours=26, tighten_window_hours=3)
    assert ns.should_notify is True
    assert ns.recheck_in_minutes == 180


def test_final_window_tightens_to_15():
    _patch(_events(2))
    ns = deadlines.notification_state(tighten_window_hours=3)
    assert ns.should_notify is True
    assert ns.recheck_in_minutes == 15


def test_season_over():
    now = datetime.now(timezone.utc)
    deadlines.fpl_api.events = lambda: [  # type: ignore
        {"id": 38, "name": "Gameweek 38",
         "deadline_time": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    ]
    ns = deadlines.notification_state()
    assert ns.deadline is None
    assert ns.should_notify is False


if __name__ == "__main__":
    test_outside_window_does_not_notify()
    test_inside_notify_window()
    test_final_window_tightens_to_15()
    test_season_over()
    print("deadline tests passed")
