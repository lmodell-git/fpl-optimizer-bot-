"""Captain choice — the single biggest swing decision of a season.

Template (highest effective-ownership) captaincy tracks the field: low variance,
roughly rank-neutral. A differential captain only pays when

  * its expected points are genuinely close to the template pick
    (within `RiskProfile.captain_diff_threshold`, typically ~1–1.5 pts), AND
  * the underlying data (fixture, xGI, secure minutes) is clearly better, AND
  * effective-ownership maths favours the punt for your rank.

This module computes that gap and only recommends off-template when it clears.
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


def choose_captain(
    squad_ids: list[int],
    projections: list[PlayerXP],
    profile: RiskProfile,
    *,
    solver_captain: int | None = None,
) -> CaptainPick:
    """Pick a captain from the XI-eligible squad, honouring the template rule."""
    idx = {p.element: p for p in projections}
    cands = [idx[e] for e in squad_ids if e in idx and idx[e].start_prob > 0.5]
    if not cands:
        cands = [idx[e] for e in squad_ids if e in idx]

    by_xp = sorted(cands, key=lambda p: p.next_gw, reverse=True)
    template = max(cands, key=lambda p: (_ownership(p.element), p.next_gw))
    top_xp = by_xp[0]

    # The template captain is whichever of {highest xP, highest owned} the field
    # will actually pile onto — approximate as the higher-owned of the top 3 xP.
    template = max(by_xp[:3], key=lambda p: _ownership(p.element))

    best = template
    gap = 0.0
    rationale = (
        f"template pick — {template.name} is both a top-xP option "
        f"({template.next_gw:.2f}) and the most-owned ({_ownership(template.element):.1f}%)"
    )

    # Is there a live differential that clears the bar?
    for cand in by_xp:
        if cand.element == template.element:
            continue
        g = template.next_gw - cand.next_gw
        own_edge = _ownership(template.element) - _ownership(cand.element)
        better_underlying = cand.start_prob >= template.start_prob and g <= profile.captain_diff_threshold
        if better_underlying and own_edge > 8.0 and profile.differential_appetite >= 0.5:
            best = cand
            gap = round(g, 2)
            rationale = (
                f"differential captain — {cand.name} is within {g:.2f} xP of template "
                f"{template.name}, at {_ownership(cand.element):.1f}% vs "
                f"{_ownership(template.element):.1f}% owned; rank profile "
                f"({profile.label}) wants the climb"
            )
            break

    if solver_captain is not None and solver_captain in idx and best.element == template.element:
        sc = idx[solver_captain]
        if sc.next_gw >= template.next_gw - 0.05:
            best = sc
            rationale = f"solver + template agree on {sc.name} ({sc.next_gw:.2f} xP next GW)"

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
