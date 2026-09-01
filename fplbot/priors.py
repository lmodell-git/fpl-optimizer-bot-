"""Prior-season per-90 rates — the shrinkage target for a thin current sample.

Without this, a player with 90 minutes played is shrunk toward a crude
positional average (every midfielder looks alike). With it, they're shrunk
toward *their own* last-season output, so the model knows a forward who scored
20 last year is not the same bet as one who scored 3 — even after two quiet
games this term.

`data/priors.json` is built by `scripts/refresh_priors.py` from the FPL
`element-summary` endpoint (last 1–2 seasons of `history_past`). Keyed by
`element_code` (the FPL id that's stable across seasons — `elements[].code` in
bootstrap-static). If the file is missing, `prior_for()` returns None and
predict.py falls back to the positional constants.
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "priors.json"
_CACHE: dict | None = None
MIN_TRUST_MINUTES = 450   # below this last-season sample, don't trust the player prior


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_PATH.read_text())
        except (FileNotFoundError, ValueError):
            _CACHE = {}
    return _CACHE


def prior_for(element_code: int | str) -> dict | None:
    """{'att_p90', 'defcon_p90', 'saves_p90', 'bonus_p90', 'start_rate',
        'minutes', 'seasons'} or None when there's no trustworthy history."""
    rec = _load().get(str(element_code))
    if not rec or rec.get("minutes", 0) < MIN_TRUST_MINUTES:
        return None
    return rec


def loaded() -> bool:
    return bool(_load())
