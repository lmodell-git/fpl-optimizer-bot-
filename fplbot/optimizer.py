"""Squad / starting-XI / captain optimisation — a Mixed Integer Program.

This is the "solved problem" layer (SPEC.md §"Optimization engine"). It builds
the best legal 15 given a budget and a per-player xP number. It's used two ways:

  * `build_initial_squad(...)` — no existing team; spend up to £100.0m.
  * `pick_xi(...)`             — squad already fixed; just choose XI + captain.

The multi-week transfer problem lives in transfers.py and reuses `_add_xi_vars`.

Solver: CBC, which ships with PuLP — no extra system dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulp

from .predict import PlayerXP

SQUAD_QUOTA = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}   # exactly 15
XI_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_TEAM = 3
DEFAULT_BUDGET = 1000  # tenths of £m


@dataclass
class SquadSolution:
    squad: list[int]          # 15 element ids
    starting_xi: list[int]    # 11 element ids
    captain: int
    vice_captain: int
    bench_order: list[int]    # 4 ids, best-xP first
    expected_points: float    # weighted objective value
    spend: int                # tenths of £m
    formation: str            # e.g. "3-4-3"
    infeasible: bool = False
    message: str = ""


def _formation(xi_pos: list[str]) -> str:
    d = sum(1 for x in xi_pos if x == "DEF")
    m = sum(1 for x in xi_pos if x == "MID")
    f = sum(1 for x in xi_pos if x == "FWD")
    return f"{d}-{m}-{f}"


def _add_xi_vars(prob, players, in_squad, xp, prefix, bench_weight):
    """Shared XI + captain sub-model. `in_squad[e]` is a binary var or 1/0 const.

    Returns (start, capt) var dicts and the XI contribution to the objective.
    """
    ids = [p.element for p in players]
    pos = {p.element: p.pos for p in players}
    start = pulp.LpVariable.dicts(f"{prefix}_start", ids, cat="Binary")
    capt = pulp.LpVariable.dicts(f"{prefix}_capt", ids, cat="Binary")

    for e in ids:
        prob += start[e] <= in_squad[e]
        prob += capt[e] <= start[e]
    prob += pulp.lpSum(start.values()) == 11
    prob += pulp.lpSum(capt.values()) == 1
    for pname, lo in XI_MIN.items():
        sel = pulp.lpSum(start[e] for e in ids if pos[e] == pname)
        prob += sel >= lo
        prob += sel <= XI_MAX[pname]

    obj = pulp.lpSum(xp[e] * start[e] for e in ids)
    obj += pulp.lpSum(xp[e] * capt[e] for e in ids)          # captain scores twice
    obj += bench_weight * pulp.lpSum(xp[e] * (in_squad[e] - start[e]) for e in ids)
    return start, capt, obj


def _solve(prob) -> bool:
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    return pulp.LpStatus[prob.status] == "Optimal"


def build_initial_squad(
    projections: list[PlayerXP],
    *,
    budget: int = DEFAULT_BUDGET,
    bench_weight: float = 0.15,
    locked_in: list[int] | None = None,
    excluded: list[int] | None = None,
) -> SquadSolution:
    """Pick the best legal 15 + XI + captain for a fresh team."""
    players = [p for p in projections if p.start_prob > 0 or (locked_in and p.element in locked_in)]
    ids = [p.element for p in players]
    xp = {p.element: p.weighted for p in players}
    pos = {p.element: p.pos for p in players}
    cost = {p.element: p.cost for p in players}
    team = {p.element: p.team for p in players}
    locked_in = set(locked_in or [])
    excluded = set(excluded or [])

    prob = pulp.LpProblem("initial_squad", pulp.LpMaximize)
    pick = pulp.LpVariable.dicts("pick", ids, cat="Binary")

    prob += pulp.lpSum(pick.values()) == 15
    prob += pulp.lpSum(cost[e] * pick[e] for e in ids) <= budget
    for pname, q in SQUAD_QUOTA.items():
        prob += pulp.lpSum(pick[e] for e in ids if pos[e] == pname) == q
    for t in set(team.values()):
        prob += pulp.lpSum(pick[e] for e in ids if team[e] == t) <= MAX_PER_TEAM
    for e in locked_in:
        if e in pick:
            prob += pick[e] == 1
    for e in excluded:
        if e in pick:
            prob += pick[e] == 0

    start, capt, xi_obj = _add_xi_vars(prob, players, pick, xp, "init", bench_weight)
    prob += xi_obj

    if not _solve(prob):
        return SquadSolution([], [], 0, 0, [], 0.0, 0, "", infeasible=True,
                             message="initial squad MIP infeasible")

    squad = [e for e in ids if pick[e].value() > 0.5]
    xi = [e for e in ids if start[e].value() > 0.5]
    cap = next(e for e in ids if capt[e].value() > 0.5)
    return _finish(squad, xi, cap, xp, pos, cost, projections)


def pick_xi(
    squad_ids: list[int],
    projections: list[PlayerXP],
    *,
    bench_weight: float = 0.15,
    use_next_gw_only: bool = True,
) -> SquadSolution:
    """Squad is fixed — choose the XI, captain, vice and bench order."""
    idx = {p.element: p for p in projections}
    players = [idx[e] for e in squad_ids if e in idx]
    ids = [p.element for p in players]
    xp = {e: (idx[e].next_gw if use_next_gw_only else idx[e].weighted) for e in ids}
    pos = {e: idx[e].pos for e in ids}
    cost = {e: idx[e].cost for e in ids}
    const_one = {e: 1 for e in ids}

    prob = pulp.LpProblem("pick_xi", pulp.LpMaximize)
    start, capt, xi_obj = _add_xi_vars(prob, players, const_one, xp, "xi", bench_weight)
    prob += xi_obj
    if not _solve(prob):
        return SquadSolution(squad_ids, [], 0, 0, [], 0.0, 0, "", infeasible=True,
                             message="XI MIP infeasible (bad squad?)")

    xi = [e for e in ids if start[e].value() > 0.5]
    cap = next(e for e in ids if capt[e].value() > 0.5)
    return _finish(squad_ids, xi, cap, xp, pos, cost, projections)


def _finish(squad, xi, cap, xp, pos, cost, projections) -> SquadSolution:
    idx = {p.element: p for p in projections}
    bench = [e for e in squad if e not in xi]
    bench.sort(key=lambda e: xp.get(e, 0.0), reverse=True)
    # Vice: best XI xP that isn't the captain.
    vice = max((e for e in xi if e != cap), key=lambda e: xp.get(e, 0.0))
    xi_pos = [pos[e] for e in xi]
    ep = (
        sum(xp.get(e, 0.0) for e in xi)
        + xp.get(cap, 0.0)
        + 0.15 * sum(xp.get(e, 0.0) for e in bench)
    )
    return SquadSolution(
        squad=squad,
        starting_xi=sorted(xi, key=lambda e: (["GKP", "DEF", "MID", "FWD"].index(pos[e]),
                                              -xp.get(e, 0.0))),
        captain=cap,
        vice_captain=vice,
        bench_order=bench,
        expected_points=round(ep, 2),
        spend=sum(cost.get(e, 0) for e in squad),
        formation=_formation(xi_pos),
    )
