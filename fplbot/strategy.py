"""Rank-aware risk knobs.

SPEC.md §"Strategy" / net takeaway: don't hardcode "safe" vs "aggressive".
The differential/template balance and captaincy variance should be a function
of current overall rank:

  * mid-table / early season  -> balanced, don't force differentials;
  * pushing from outside the pack toward the top 1% -> you mathematically need
    differential exposure, because full-template = finish where the field
    average finishes.

Top ~1% is roughly rank 50k–100k of ~11m. These curves are deliberately
simple and live here so they're easy to re-tune.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

TOTAL_PLAYERS = 11_000_000


@dataclass
class RiskProfile:
    rank: int | None
    percentile: float                # 0.0 = rank 1, 1.0 = last
    differential_appetite: float      # 0..1 — how far to chase low-ownership upside
    captain_diff_threshold: float     # xP gap (pts) within which a differential captain is allowed
    max_hit_chase: int                # extra -4 hits the bot may take chasing rank
    label: str

    def as_predict_overrides(self) -> dict:
        """Feed into the objective: nudge ownership handling without rewriting predict."""
        return {
            "differential_bonus": 0.10 + 0.45 * self.differential_appetite,
            "template_penalty": 0.05 * (1.0 - self.differential_appetite),
        }


def templatise(projections, *, min_start_prob: float = 0.6, xp_weight: float = 0.35):
    """Re-score projections so the optimiser builds a low-variance, template-owned
    squad: effective ownership dominates, xP is only a tiebreak, and anyone below
    `min_start_prob` is dropped. Returns copies — the originals keep their real xP.
    """
    from . import fpl_api

    pl = fpl_api.players_by_id()

    def own(el: int) -> float:
        try:
            return float(pl.get(el, {}).get("selected_by_percent") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    mx = max((p.weighted for p in projections), default=1.0) or 1.0
    out = []
    for p in projections:
        q = copy.copy(p)
        q.weighted = -1.0 if p.start_prob < min_start_prob else (
            own(p.element) + xp_weight * (p.weighted / mx * 100.0)
        )
        out.append(q)
    return out


def _percentile(rank: int | None) -> float:
    if not rank or rank <= 0:
        return 0.5
    return min(1.0, rank / TOTAL_PLAYERS)


def profile_for_rank(rank: int | None, *, gameweek: int = 1) -> RiskProfile:
    pct = _percentile(rank)

    # Early in the season, damp the aggression regardless of rank — small sample.
    early = max(0.4, min(1.0, gameweek / 8.0))

    if rank is None:
        appetite = 0.35
        label = "no rank yet — balanced"
    elif pct <= 0.009:            # already top ~1%
        appetite = 0.30
        label = f"top 1% (rank {rank:,}) — protect, template-lean"
    elif pct <= 0.05:            # top 5%, knocking on the door
        appetite = 0.55
        label = f"top 5% (rank {rank:,}) — measured differentials to climb"
    elif pct <= 0.20:
        appetite = 0.70
        label = f"top 20% (rank {rank:,}) — need differential exposure to close the gap"
    else:
        appetite = 0.50
        label = f"rank {rank:,} — balanced rebuild, don't punt wildly"

    appetite *= early
    return RiskProfile(
        rank=rank,
        percentile=round(pct, 5),
        differential_appetite=round(appetite, 3),
        captain_diff_threshold=round(1.0 + 1.5 * appetite, 2),
        max_hit_chase=0 if appetite < 0.4 else (1 if appetite < 0.65 else 2),
        label=label,
    )
