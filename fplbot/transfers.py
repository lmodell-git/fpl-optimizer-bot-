"""Multi-period transfer + chip optimisation.

A greedy single-week transfer look is what separates a reactive manager from a
season-winning one (SPEC.md §"Optimization engine"). This solves one MIP over a
rolling horizon of H gameweeks:

  * free transfers bank up to 5, an extra transfer costs 4 pts;
  * buy/sell prices from state (selling price includes the FPL profit rule);
  * starting XI + captain chosen per GW;
  * chips (wildcard / free hit / bench boost / triple captain) are evaluated as
    part of the same horizon, not week by week — `evaluate_chip_options()`
    re-solves with each chip slotted into each near GW and ranks the deltas.

Price *changes* over the horizon are not modelled (the API doesn't predict
them reliably); `now_cost` is used for buys. Free Hit is approximated as
"one GW of unlimited free transfers, squad reverts after" — the solver treats
it like a one-week wildcard and the caller is told not to persist that GW.

Player universe is pruned to the top-N per position by weighted xP plus every
currently-owned player, keeping CBC solve times in the low seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pulp

from .optimizer import MAX_PER_TEAM, SQUAD_QUOTA, XI_MAX, XI_MIN
from .predict import PlayerXP
from .state import MAX_FREE_TRANSFERS, TeamState

HIT_COST = 4
BIG_M = 20


@dataclass
class GWPlan:
    event: int
    transfers_in: list[int]
    transfers_out: list[int]
    starting_xi: list[int]
    captain: int
    vice_captain: int
    bench_order: list[int]
    chip: str | None
    hits: int
    free_transfers_before: int
    expected_points: float


@dataclass
class TransferPlan:
    horizon: list[int]
    per_gw: list[GWPlan]
    objective: float
    chip_used: dict[int, str] = field(default_factory=dict)  # event -> chip
    infeasible: bool = False
    message: str = ""

    @property
    def next_gw(self) -> GWPlan | None:
        return self.per_gw[0] if self.per_gw else None


def _pool(state: TeamState, projections: list[PlayerXP], cfg: dict) -> list[PlayerXP]:
    pc = (cfg or {}).get("transfers", {})
    caps = {
        "GKP": pc.get("pool_gkp", 8),
        "DEF": pc.get("pool_def", 32),
        "MID": pc.get("pool_mid", 36),
        "FWD": pc.get("pool_fwd", 22),
    }
    owned = set(state.squad_ids)
    chosen: dict[int, PlayerXP] = {}
    per_pos: dict[str, int] = {k: 0 for k in caps}
    for p in projections:  # already sorted by weighted xP desc
        keep = p.element in owned
        if not keep and per_pos[p.pos] < caps[p.pos] and (p.start_prob > 0):
            per_pos[p.pos] += 1
            keep = True
        if keep:
            chosen[p.element] = p
    # Make sure every owned id is present even if proja dropped it.
    idx = {p.element: p for p in projections}
    for e in owned:
        if e not in chosen and e in idx:
            chosen[e] = idx[e]
    return list(chosen.values())


def _sell_price(state: TeamState) -> dict[int, int]:
    return {pk.element: pk.selling_price for pk in state.squad}


def solve_horizon(
    state: TeamState,
    projections: list[PlayerXP],
    horizon_events: list[int],
    cfg: dict | None = None,
    *,
    chip_schedule: dict[int, str] | None = None,
) -> TransferPlan:
    cfg = cfg or {}
    tp = cfg.get("transfers", {})
    decay = tp.get("decay", 0.84)
    bench_weight = tp.get("bench_weight", 0.15)
    chip_schedule = dict(chip_schedule or {})

    players = _pool(state, projections, cfg)
    ids = [p.element for p in players]
    pos = {p.element: p.pos for p in players}
    team = {p.element: p.team for p in players}
    cost = {p.element: p.cost for p in players}
    idx = {p.element: p for p in players}
    sell = _sell_price(state)
    owned0 = set(state.squad_ids)
    have_team = state.has_team

    def xp(e: int, gw_pos: int) -> float:
        pg = idx[e].per_gw
        return pg[gw_pos] if gw_pos < len(pg) else (pg[-1] if pg else 0.0)

    H = list(range(len(horizon_events)))
    prob = pulp.LpProblem("transfer_horizon", pulp.LpMaximize)

    own = pulp.LpVariable.dicts("own", (ids, H), cat="Binary")
    buy = pulp.LpVariable.dicts("buy", (ids, H), cat="Binary")
    sel = pulp.LpVariable.dicts("sell", (ids, H), cat="Binary")
    start = pulp.LpVariable.dicts("start", (ids, H), cat="Binary")
    capt = pulp.LpVariable.dicts("capt", (ids, H), cat="Binary")
    ft = pulp.LpVariable.dicts("ft", H, lowBound=1, upBound=MAX_FREE_TRANSFERS)
    hits = pulp.LpVariable.dicts("hits", H, lowBound=0, cat="Integer")
    took_hit = pulp.LpVariable.dicts("took_hit", H, cat="Binary")

    obj_terms = []
    for g in H:
        ev = horizon_events[g]
        chip = chip_schedule.get(ev)
        free_week = chip in ("wildcard", "freehit")
        bw = 1.0 if chip == "bboost" else bench_weight
        cap_mult = 2.0 if chip == "3xc" else 1.0  # captain scores (cap_mult)x extra

        # --- squad transition -------------------------------------------- #
        for e in ids:
            prev = (own[e][g - 1] if g > 0 else (1 if e in owned0 else 0))
            prob += own[e][g] == prev + buy[e][g] - sel[e][g]
            prob += buy[e][g] + sel[e][g] <= 1

        prob += pulp.lpSum(own[e][g] for e in ids) == 15
        for pname, q in SQUAD_QUOTA.items():
            prob += pulp.lpSum(own[e][g] for e in ids if pos[e] == pname) == q
        for t in set(team.values()):
            prob += pulp.lpSum(own[e][g] for e in ids if team[e] == t) <= MAX_PER_TEAM

        # --- budget ---------------------------------------------------------- #
        buy_spend = pulp.lpSum(cost[e] * buy[e][g] for e in ids)
        sell_income = pulp.lpSum(sell.get(e, cost[e]) * sel[e][g] for e in ids)
        if g == 0:
            prob += buy_spend - sell_income <= state.bank
        else:
            # Rolling bank: can't spend more than what earlier sales freed up.
            cum_in = pulp.lpSum(
                cost[e] * buy[e][gg] for e in ids for gg in range(g + 1)
            )
            cum_out = pulp.lpSum(
                sell.get(e, cost[e]) * sel[e][gg] for e in ids for gg in range(g + 1)
            )
            prob += cum_in - cum_out <= state.bank

        # --- free transfers & hits ---------------------------------------- #
        tmade = pulp.lpSum(buy[e][g] for e in ids)
        if free_week:
            prob += hits[g] == 0
            prob += took_hit[g] == 0
        else:
            prob += hits[g] >= tmade - ft[g]
            prob += tmade - ft[g] <= BIG_M * took_hit[g]
            prob += hits[g] <= BIG_M * took_hit[g]
        if g + 1 in H and not free_week:
            prob += ft[g + 1] <= ft[g] - tmade + 1 + BIG_M * took_hit[g]
            prob += ft[g + 1] <= 1 + BIG_M * (1 - took_hit[g])
        elif g + 1 in H and free_week:
            prob += ft[g + 1] <= 2  # a played chip doesn't consume the saved FT
        if g == 0:
            prob += ft[0] == min(MAX_FREE_TRANSFERS, max(1, state.free_transfers))

        # --- XI + captain ------------------------------------------------- #
        for e in ids:
            prob += start[e][g] <= own[e][g]
            prob += capt[e][g] <= start[e][g]
        prob += pulp.lpSum(start[e][g] for e in ids) == 11
        prob += pulp.lpSum(capt[e][g] for e in ids) == 1
        for pname in XI_MIN:
            sel_pos = pulp.lpSum(start[e][g] for e in ids if pos[e] == pname)
            prob += sel_pos >= XI_MIN[pname]
            prob += sel_pos <= XI_MAX[pname]

        w = decay ** g
        gw_obj = pulp.lpSum(xp(e, g) * start[e][g] for e in ids)
        gw_obj += cap_mult * pulp.lpSum(xp(e, g) * capt[e][g] for e in ids)
        gw_obj += bw * pulp.lpSum(xp(e, g) * (own[e][g] - start[e][g]) for e in ids)
        gw_obj -= HIT_COST * hits[g]
        obj_terms.append(w * gw_obj)

    # Discourage churn: a free transfer must clear a small threshold of xP gain,
    # otherwise the solver ping-pongs near-equal players around the horizon.
    churn_penalty = tp.get("churn_penalty", 0.30)
    obj_terms.append(-churn_penalty * pulp.lpSum(buy[e][g] for e in ids for g in H))
    prob += pulp.lpSum(obj_terms)

    if not have_team:
        return TransferPlan(horizon_events, [], 0.0, infeasible=True,
                            message="no existing squad — use optimizer.build_initial_squad")

    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=tp.get("time_limit", 90)))
    if pulp.LpStatus[prob.status] not in ("Optimal",):
        return TransferPlan(horizon_events, [], 0.0, infeasible=True,
                            message=f"solver status: {pulp.LpStatus[prob.status]}")

    per_gw: list[GWPlan] = []
    prev_squad = set(owned0)
    for g in H:
        ev = horizon_events[g]
        squad_g = {e for e in ids if own[e][g].value() > 0.5}
        xi = [e for e in ids if start[e][g].value() > 0.5]
        cap = next(e for e in ids if capt[e][g].value() > 0.5)
        ins = sorted(squad_g - prev_squad)
        outs = sorted(prev_squad - squad_g)
        bench = [e for e in squad_g if e not in xi]
        bench.sort(key=lambda e: xp(e, g), reverse=True)
        vice = max((e for e in xi if e != cap), key=lambda e: xp(e, g))
        order = ["GKP", "DEF", "MID", "FWD"]
        per_gw.append(GWPlan(
            event=ev,
            transfers_in=ins,
            transfers_out=outs,
            starting_xi=sorted(xi, key=lambda e: (order.index(pos[e]), -xp(e, g))),
            captain=cap,
            vice_captain=vice,
            bench_order=bench,
            chip=chip_schedule.get(ev),
            hits=int(round(hits[g].value() or 0)),
            free_transfers_before=int(round(ft[g].value() or 1)),
            expected_points=round(
                sum(xp(e, g) for e in xi) + xp(cap, g)
                - HIT_COST * (hits[g].value() or 0), 2),
        ))
        prev_squad = squad_g

    return TransferPlan(
        horizon=horizon_events,
        per_gw=per_gw,
        objective=round(pulp.value(prob.objective), 2),
        chip_used=dict(chip_schedule),
    )


def recommend_transfers(
    state: TeamState,
    projections: list[PlayerXP],
    horizon_events: list[int],
    cfg: dict | None = None,
) -> TransferPlan:
    """The no-chip baseline plan for the horizon."""
    return solve_horizon(state, projections, horizon_events, cfg, chip_schedule=None)


@dataclass
class ChipOption:
    chip: str
    event: int
    objective: float
    delta_vs_baseline: float
    plan: TransferPlan


def evaluate_chip_options(
    state: TeamState,
    projections: list[PlayerXP],
    horizon_events: list[int],
    cfg: dict | None = None,
    *,
    chips: tuple[str, ...] = ("wildcard", "freehit", "bboost", "3xc"),
    only_first_n: int = 3,
) -> tuple[TransferPlan, list[ChipOption]]:
    """Baseline + every available chip in each of the next `only_first_n` GWs, ranked."""
    baseline = recommend_transfers(state, projections, horizon_events, cfg)
    base_obj = baseline.objective if not baseline.infeasible else float("-inf")

    options: list[ChipOption] = []
    for chip in chips:
        if not state.chip_available(chip if chip != "3xc" else "3xc"):
            continue
        for ev in horizon_events[:only_first_n]:
            plan = solve_horizon(
                state, projections, horizon_events, cfg, chip_schedule={ev: chip}
            )
            if plan.infeasible:
                continue
            options.append(ChipOption(
                chip=chip, event=ev, objective=plan.objective,
                delta_vs_baseline=round(plan.objective - base_obj, 2), plan=plan,
            ))
    options.sort(key=lambda o: o.delta_vs_baseline, reverse=True)
    return baseline, options
