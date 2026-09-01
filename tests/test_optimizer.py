"""Optimiser constraint tests — run with: python -m pytest tests/ (or python tests/test_optimizer.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplbot.optimizer import (  # noqa: E402
    MAX_PER_TEAM, SQUAD_QUOTA, XI_MAX, XI_MIN, build_initial_squad, pick_xi,
)
from fplbot.predict import PlayerXP  # noqa: E402


def _synthetic_universe():
    """A small, legal-by-construction player pool: 6 teams, priced so £100m binds."""
    players, eid = [], 1
    plan = {"GKP": 6, "DEF": 12, "MID": 12, "FWD": 8}
    for pos, n in plan.items():
        for i in range(n):
            players.append(PlayerXP(
                element=eid, name=f"{pos}{i}", pos=pos, team=(eid % 6) + 1,
                cost=45 + (i % 7) * 5,
                per_gw=[5.0 - (i * 0.15)] * 5,
                start_prob=0.9,
                weighted=sum((5.0 - i * 0.15) * (0.84 ** k) for k in range(5)),
            ))
            eid += 1
    return players


def test_initial_squad_respects_all_constraints():
    sol = build_initial_squad(_synthetic_universe(), budget=1000)
    assert not sol.infeasible, sol.message
    assert len(sol.squad) == 15
    assert len(set(sol.squad)) == 15
    assert len(sol.starting_xi) == 11
    assert sol.captain in sol.starting_xi
    assert sol.vice_captain in sol.starting_xi and sol.vice_captain != sol.captain
    assert sol.spend <= 1000

    idx = {p.element: p for p in _synthetic_universe()}
    by_pos = {}
    for e in sol.squad:
        by_pos[idx[e].pos] = by_pos.get(idx[e].pos, 0) + 1
    assert by_pos == SQUAD_QUOTA

    team_counts = {}
    for e in sol.squad:
        t = idx[e].team
        team_counts[t] = team_counts.get(t, 0) + 1
    assert max(team_counts.values()) <= MAX_PER_TEAM

    xi_pos = {}
    for e in sol.starting_xi:
        xi_pos[idx[e].pos] = xi_pos.get(idx[e].pos, 0) + 1
    for p, lo in XI_MIN.items():
        assert lo <= xi_pos.get(p, 0) <= XI_MAX[p]


def test_pick_xi_on_fixed_squad():
    universe = _synthetic_universe()
    sol0 = build_initial_squad(universe, budget=1000)
    sol1 = pick_xi(sol0.squad, universe)
    assert not sol1.infeasible
    assert set(sol1.squad) == set(sol0.squad)
    assert len(sol1.starting_xi) == 11
    assert sol1.captain in sol1.starting_xi


def test_budget_binds():
    """A tiny budget must make the problem infeasible, not silently overspend."""
    sol = build_initial_squad(_synthetic_universe(), budget=200)
    assert sol.infeasible


if __name__ == "__main__":
    test_initial_squad_respects_all_constraints()
    test_pick_xi_on_fixed_squad()
    test_budget_binds()
    print("optimizer tests passed")
