"""Captain choice — the single biggest swing decision of a season.

The armband doubles a player's expected points, so the pick is the highest
next-GW xP among nailed starters. Two tie-breaks only:

  * within PRICE_TIEBREAK_XP, the pricier player (price is the market's view of
    quality, and the model's top-end projections run optimistic), and
  * within DIFFERENTIAL_TIE_XP, a much lower-owned option when the rank profile
    wants to climb — never a knowingly lower-xP punt.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import fpl_api
from .predict import PlayerXP
from .strategy import RiskProfile


@dataclass
class CaptainPick:
    element: int
    name: str
    is_template: bool
    template_element: int
    template_name: str
    xp_gap: float                 # template xP − pick xP (>=0)
    template_ownership: float     # selected_by_percent
    pick_ownership: float
    rationale: str


def _ownership(el_id: int) -> float:
    p = fpl_api.players_by_id().get(el_id, {})
    try:
        return float(p.get("selected_by_percent") or 0.0)
    except ValueError:
        return 0.0


# Captaincy is an expected-points decision: the armband doubles the mean, so a
# lower-xP "differential" is a straight EV loss unless it's essentially tied.
NAILED_START_PROB = 0.75   # a benched captain hands the armband to the vice
PRICE_TIEBREAK_XP = 0.5    # within this, prefer the pricier (market-rated) player
DIFFERENTIAL_TIE_XP = 0.25  # a lower-owned pick only when it's this close


def choose_captain(
    squad_ids: list[int],
    projections: list[PlayerXP],
    profile: RiskProfile,
    *,
    solver_captain: int | None = None,
) -> CaptainPick:
    """Highest next-GW xP among nailed starters, with price / ownership tie-breaks."""
    idx = {p.element: p for p in projections}
    squad = [idx[e] for e in squad_ids if e in idx]
    cands = ([p for p in squad if p.start_prob >= NAILED_START_PROB]
             or [p for p in squad if p.start_prob > 0.5] or squad)

    top_xp = max(p.next_gw for p in cands)
    near = [p for p in cands if p.next_gw >= top_xp - PRICE_TIEBREAK_XP]
    best = max(near, key=lambda p: (p.cost, p.next_gw))
    # "Template" = the most-owned of the near-top options — what the field captains.
    template = max(near, key=lambda p: (_ownership(p.element), p.next_gw))

    if len(near) > 1 and best.element != max(near, key=lambda p: p.next_gw).element:
        rationale = (f"{best.name} ({best.next_gw:.2f} xP) — within {PRICE_TIEBREAK_XP} xP of "
                     f"the top projection, so the pricier, more proven player gets it")
    else:
        rationale = f"{best.name} — highest projection among nailed starters ({best.next_gw:.2f} xP)"

    # Differential: only an essentially-tied, much less-owned option, and only
    # when the rank profile wants to climb.
    gap = 0.0
    if profile.differential_appetite >= 0.5:
        for cand in sorted(cands, key=lambda p: -p.next_gw):
            g = best.next_gw - cand.next_gw
            if cand.element == best.element or g > DIFFERENTIAL_TIE_XP:
                continue
            if _ownership(best.element) - _ownership(cand.element) > 8.0:
                rationale = (f"differential captain — {cand.name} is within {g:.2f} xP of "
                             f"{best.name}, at {_ownership(cand.element):.1f}% vs "
                             f"{_ownership(best.element):.1f}% owned ({profile.label})")
                best, gap = cand, round(g, 2)
                break

    return CaptainPick(
        element=best.element,
        name=best.name,
        is_template=best.element == template.element,
        template_element=template.element,
        template_name=template.name,
        xp_gap=gap,
        template_ownership=_ownership(template.element),
        pick_ownership=_ownership(best.element),
        rationale=rationale,
    )
