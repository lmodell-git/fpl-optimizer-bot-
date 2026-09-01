"""Read-only client for the official Fantasy Premier League API.

All endpoints here are the publicly documented (unauthenticated) ones —
`fantasy.premierleague.com/api/...`. No login, no cookies. Writing a team
(transfers / captain) needs the unofficial authenticated endpoints and is
deliberately NOT in this file; see SPEC.md §"Deferred: auto-execution".

Everything is cached in-process for the lifetime of one run so the pipeline
can pull `bootstrap_static()` from a dozen places without re-fetching.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any

BASE = "https://fantasy.premierleague.com/api"

# A browser-ish UA — the API 403s some default library agents.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_TIMEOUT = 20
_RETRIES = 3
_BACKOFF = 2.0


class NotFound(RuntimeError):
    """The resource doesn't exist (HTTP 404) — e.g. picks for a GW not yet public."""


def _get(path: str) -> Any:
    """GET {BASE}{path} as JSON. Retries transient failures; 404 raises NotFound."""
    url = f"{BASE}{path}"
    last_err: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound(f"404: {url}") from exc
            last_err = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
        if attempt < _RETRIES - 1:
            time.sleep(_BACKOFF * (attempt + 1))
    raise RuntimeError(f"FPL API GET failed after {_RETRIES} tries: {url} ({last_err})")


@lru_cache(maxsize=1)
def bootstrap_static() -> dict:
    """The big one: elements (players), teams, element_types, events, chips, game_settings."""
    return _get("/bootstrap-static/")


@lru_cache(maxsize=1)
def fixtures() -> list[dict]:
    """All fixtures for the season, with per-team FDR and kickoff times."""
    return _get("/fixtures/")


@lru_cache(maxsize=1)
def event_status() -> dict:
    """Bonus-added / league-update status for the in-flight gameweek."""
    return _get("/event-status/")


@lru_cache(maxsize=1024)
def element_summary(element_id: int) -> dict:
    """Per-player history: `history` (this season, per GW) + `fixtures` (upcoming)."""
    return _get(f"/element-summary/{element_id}/")


@lru_cache(maxsize=64)
def entry(entry_id: int) -> dict:
    """Manager summary: name, overall rank/points, current event."""
    return _get(f"/entry/{entry_id}/")


@lru_cache(maxsize=64)
def entry_history(entry_id: int) -> dict:
    """Manager season history: per-GW points/rank, past seasons, chips used."""
    return _get(f"/entry/{entry_id}/history/")


def entry_picks(entry_id: int, event: int) -> dict:
    """The 15 picks (+ captain, bench order, bank, value) for one gameweek.

    Not cached — you generally ask for exactly one (entry, event) pair.
    """
    return _get(f"/entry/{entry_id}/event/{event}/picks/")


def clear_cache() -> None:
    """Drop every in-process cache. Call at the top of a fresh pipeline run."""
    for fn in (
        bootstrap_static,
        fixtures,
        event_status,
        element_summary,
        entry,
        entry_history,
    ):
        fn.cache_clear()


# --------------------------------------------------------------------------- #
# Small conveniences on top of bootstrap-static                               #
# --------------------------------------------------------------------------- #

POS_BY_TYPE = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def players_by_id() -> dict[int, dict]:
    return {p["id"]: p for p in bootstrap_static()["elements"]}


def teams_by_id() -> dict[int, dict]:
    return {t["id"]: t for t in bootstrap_static()["teams"]}


def events() -> list[dict]:
    return bootstrap_static()["events"]


def next_event() -> dict:
    """The next gameweek that has not finished (falls back to the last event)."""
    evs = events()
    for ev in evs:
        if ev.get("is_next"):
            return ev
    for ev in evs:
        if not ev["finished"]:
            return ev
    return evs[-1]


def current_event() -> dict | None:
    for ev in events():
        if ev.get("is_current"):
            return ev
    return None
