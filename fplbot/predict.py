"""Expected-points model.

The optimiser is only as good as the per-player number it maximises. FPL's own
`form` column is noisy and backward-looking, so this builds a forward estimate:

    xP(player, gw) = P(appears) * appearance_pts
                   + minutes_factor * (attacking_pts_p90 * fixture_att_mult
                                       + defensive_pts_p90 * fixture_def_mult
                                       + bonus_pts_p90)
                   - discipline_pts_p90 * minutes_factor

Key ideas (SPEC.md §"Prediction layer"):
  * Expected minutes / start probability is the biggest driver of variance —
    a nailed starter beats a rotation-risk star. Driven by `status`,
    `chance_of_playing_next_round`, and season minutes consistency.
  * Underlying xG/xA over actual G/A — regression to the mean is real.
  * Set-piece duty (penalties especially) adds a floor the open-play xG misses.
  * Team attack vs opponent defence, split home/away — not raw FDR.

`ep_next` from the API is blended in as a sanity anchor (`api_anchor_weight`).

Everything tunable lives in config.yaml -> `predict:`; `Params` mirrors it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from . import fpl_api, priors

# FPL scoring: points for a goal / clean sheet, by position.
GOAL_PTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
ASSIST_PTS = 3
CS_PTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
# Defensive-contribution points (2 pts) once per match above a per-position threshold.
DEFCON_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12, "GKP": 999}
DEFCON_PTS = 2


@dataclass
class Params:
    horizon: int = 5                 # gameweeks to look ahead
    decay: float = 0.84              # weight of GW n+k is decay**k
    api_anchor_weight: float = 0.10  # blend toward FPL's own ep_next (≈ form; weak predictor)
    bench_weight: float = 0.15       # how much bench xP counts in the objective
    min_minutes_full: int = 60       # "played 60+" threshold for the 2nd appearance pt
    penalty_bonus_p90: float = 0.9   # extra xP/90 for the nailed penalty taker
    corner_bonus_p90: float = 0.15   # extra xP/90 for the primary corner/FK taker
    bonus_scale: float = 0.9         # multiplier on the ICT-derived bonus estimate
    home_advantage: float = 1.08     # attacking multiplier at home (÷ for away)
    fixture_swing: float = 0.45      # how hard team/opponent strength bends returns
    doubt_penalty: float = 0.5       # xP multiplier for status 'd' with no % given
    shrink_minutes: int = 300        # per-90 rates shrink toward a positional prior below this sample
    max_fixture_xp: float = 13.0     # hard clamp on any single-fixture xP (blow-up guard)

    @staticmethod
    def from_config(cfg: dict) -> "Params":
        p = Params()
        for k, v in (cfg or {}).get("predict", {}).items():
            if hasattr(p, k):
                setattr(p, k, v)
        return p


@dataclass
class PlayerXP:
    element: int
    name: str
    pos: str
    team: int
    cost: int                       # now_cost, tenths of £m
    per_gw: list[float]             # xP for each GW in the horizon
    start_prob: float
    weighted: float = 0.0           # decay-weighted sum across the horizon
    notes: list[str] = field(default_factory=list)

    @property
    def next_gw(self) -> float:
        return self.per_gw[0] if self.per_gw else 0.0


# --------------------------------------------------------------------------- #
# Fixture handling                                                            #
# --------------------------------------------------------------------------- #

def team_fixtures_by_event(horizon_events: Iterable[int]) -> dict[int, dict[int, list[dict]]]:
    """{event: {team_id: [fixture, ...]}} — a team can have 0 (blank) or 2+ (double)."""
    want = set(horizon_events)
    out: dict[int, dict[int, list[dict]]] = {e: {} for e in want}
    for fx in fpl_api.fixtures():
        ev = fx["event"]
        if ev not in want or fx["finished"]:
            continue
        for side, opp_side, home in (("team_h", "team_a", True), ("team_a", "team_h", False)):
            rec = {
                "opponent": fx[opp_side],
                "home": home,
                "difficulty": fx["team_h_difficulty" if home else "team_a_difficulty"],
            }
            out[ev].setdefault(fx[side], []).append(rec)
    return out


LEAGUE_GOALS = 1.37   # mean goals per team per PL match


def team_ratings(boot: dict, fixtures: list[dict]) -> dict[int, tuple[float, float]]:
    """{team: (attack, defence)} as expected goals scored / conceded per match.

    This season's xG for (sum of players' xG) and xG against (from the
    team's most-used player's xGC/90 — normally the keeper), shrunk toward a
    prior from FPL's 2–5 team strength while the sample is small.
    """
    games: dict[int, int] = {}
    for fx in fixtures:
        if fx.get("finished"):
            for side in ("team_h", "team_a"):
                games[fx[side]] = games.get(fx[side], 0) + 1
    xg: dict[int, float] = {}
    xgc90: dict[int, tuple[float, float]] = {}   # team -> (minutes, xGC/90) of most-used player
    for p in boot["elements"]:
        t = p["team"]
        xg[t] = xg.get(t, 0.0) + _f(p, "expected_goals")
        m = _f(p, "minutes")
        if m > xgc90.get(t, (0.0, 0.0))[0]:
            xgc90[t] = (m, _f(p, "expected_goals_conceded") * 90.0 / m)
    out = {}
    for t in boot["teams"]:
        tid = t["id"]
        s = (t.get("strength") or t.get("strength_overall_home") or 3) / 3.0
        prior_att, prior_def = LEAGUE_GOALS * s ** 0.9, LEAGUE_GOALS / s ** 0.9
        n, k = games.get(tid, 0), 5.0
        att = (xg.get(tid, 0.0) + k * prior_att) / (n + k) if n else prior_att
        d_obs = xgc90.get(tid, (0.0, 0.0))[1]
        dfn = (n * d_obs + k * prior_def) / (n + k) if n and d_obs else prior_def
        out[tid] = (att, dfn)
    return out


def _expected_goals(att: float, opp_def: float, home: bool, params: "Params") -> float:
    lam = att * opp_def / LEAGUE_GOALS
    return lam * (params.home_advantage if home else 1.0 / params.home_advantage)


def _poisson_half_conceded(lam: float) -> float:
    """E[floor(goals/2)] for Poisson goals — the DEF/GKP -1 per 2 conceded."""
    import math
    tot, pk = 0.0, math.exp(-lam)
    for k in range(0, 12):
        if k:
            pk *= lam / k
        tot += (k // 2) * pk
    return tot


# --------------------------------------------------------------------------- #
# Minutes model                                                              #
# --------------------------------------------------------------------------- #

def recent_histories(players: list[dict]) -> dict[int, list[dict]]:
    """{element: per-fixture history rows, oldest first} for anyone who has played.

    One element-summary call per player (concurrent). A player with 0 minutes
    has no starts to read, so they're skipped. Failures just drop the player —
    the minutes model then falls back to season totals.
    """
    import concurrent.futures as cf
    ids = [p["id"] for p in players if _f(p, "minutes") > 0]
    out: dict[int, list[dict]] = {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fpl_api.element_summary, i): i for i in ids}
        for fut in cf.as_completed(futs):
            try:
                rows = fut.result().get("history", [])
            except Exception:  # noqa: BLE001
                continue
            out[futs[fut]] = sorted(rows, key=lambda r: (r["round"], r.get("kickoff_time") or ""))
    return out


# Weights on the player's last three team fixtures, newest first.
RECENT_WEIGHTS = (0.5, 0.3, 0.2)


def _start_probability(p: dict, games_so_far: int, params: Params,
                       prior: dict | None,
                       recent: list[dict] | None = None) -> tuple[float, list[str]]:
    notes: list[str] = []
    status = p.get("status", "a")
    chance = p.get("chance_of_playing_next_round")

    if status in ("i", "s", "u", "n") or chance == 0:
        return 0.0, [f"status={status} chance={chance} → out"]
    if chance is not None:
        base = chance / 100.0
        if chance < 100:
            notes.append(f"chance_of_playing={chance}%")
    elif status == "d":
        base = params.doubt_penalty
        notes.append("flagged doubtful, no % given")
    else:
        base = 1.0

    # Minutes consistency: this season's starts/minutes — weighted toward the
    # last few fixtures (losing your place is the strongest signal there is) —
    # shrunk toward last season's start rate while the sample is thin.
    mins = _f(p, "minutes")
    starts = _f(p, "starts")
    prior_rate = prior["start_rate"] if prior else 0.45
    if games_so_far > 0:
        w = min(1.0, games_so_far / 4.0)
        this_rate = 0.55 * min(1.0, starts / games_so_far) + 0.45 * min(1.0, mins / (games_so_far * 90.0))
        last = (recent or [])[-len(RECENT_WEIGHTS):][::-1]
        if last:
            ws = RECENT_WEIGHTS[:len(last)]
            rec = sum(wt * min(1.0, _f(r, "minutes") / 90.0) for wt, r in zip(ws, last)) / sum(ws)
            this_rate = 0.4 * this_rate + 0.6 * rec
        elif mins == 0:
            this_rate = 0.0
        consistency = w * this_rate + (1 - w) * prior_rate
        if consistency < 0.5 and w > 0.4:
            notes.append(f"started {int(starts)}/{games_so_far} — rotation risk")
    else:
        consistency = prior_rate
        notes.append("pre-season — using last-year start rate" if prior
                     else "no history — minutes uncertain")
    base *= consistency
    return max(0.0, min(1.0, base)), notes


# --------------------------------------------------------------------------- #
# Per-90 point rates                                                         #
# --------------------------------------------------------------------------- #

def _f(p: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(p.get(key) or default)
    except (TypeError, ValueError):
        return default


# "Average starter" per-90 point rates, used as the shrinkage target when a
# player's own sample is thin (early season, squad rotation, new signing).
ATT_PRIOR_P90 = {"GKP": 0.05, "DEF": 0.55, "MID": 1.45, "FWD": 2.35}
DEFCON_PRIOR_P90 = {"GKP": 0.0, "DEF": 7.5, "MID": 6.0, "FWD": 3.0}


def _shrink(raw: float, minutes: float, prior: float, k: float) -> float:
    """James–Stein style: weight the observed rate by sample size, else the prior.

    minutes -> inf  gives raw; minutes -> 0 gives prior; k is the half-weight point.
    """
    w = minutes / (minutes + k) if minutes > 0 else 0.0
    return w * raw + (1.0 - w) * prior


def _attacking_p90(p: dict, pos: str, params: Params,
                   prior: dict | None) -> tuple[float, list[str]]:
    notes: list[str] = []
    minutes = _f(p, "minutes")
    xg90 = _f(p, "expected_goals_per_90")
    xa90 = _f(p, "expected_assists_per_90")
    # Raw involvement rate as a secondary signal (also per-90, also noisy).
    if minutes > 0:
        g90 = _f(p, "goals_scored") * 90.0 / minutes
        a90 = _f(p, "assists") * 90.0 / minutes
        xg90 = xg90 or g90
        xa90 = xa90 or a90
        # A touch of actual finishing on top of the xG model (over/under-performers).
        xg90 = 0.85 * xg90 + 0.15 * g90
        xa90 = 0.85 * xa90 + 0.15 * a90

    raw = xg90 * GOAL_PTS[pos] + xa90 * ASSIST_PTS
    target = prior["att_p90"] if prior else ATT_PRIOR_P90[pos]
    pts = _shrink(raw, minutes, target, params.shrink_minutes)
    if minutes < params.shrink_minutes:
        notes.append(f"{int(minutes)} min — shrunk toward "
                     + ("own last-season rate" if prior else f"{pos} prior"))

    # Set-piece duty is a role, not a sample — add it after shrinkage.
    if str(p.get("penalties_order") or "") == "1":
        pts += params.penalty_bonus_p90
        notes.append("penalties")
    if str(p.get("corners_and_indirect_freekicks_order") or "") == "1" or \
       str(p.get("direct_freekicks_order") or "") == "1":
        pts += params.corner_bonus_p90
        notes.append("set-pieces")
    return pts, notes


def _defensive_p90(p: dict, pos: str, lam_against: float, params: Params,
                   prior: dict | None) -> float:
    import math
    minutes = _f(p, "minutes")
    if pos == "FWD":
        cs_component = 0.0
    else:
        # Poisson clean-sheet chance from this fixture's expected goals against;
        # DEF/GKP also lose 1 per 2 conceded.
        cs_component = math.exp(-lam_against) * CS_PTS[pos]
        if pos in ("DEF", "GKP"):
            cs_component -= _poisson_half_conceded(lam_against)

    # Defensive-contribution points: per-90 CBIT/tackles/recoveries, shrunk hard
    # (a 1-minute cameo can report defcon90 = 90).
    defcon90 = _f(p, "defensive_contribution_per_90")
    if defcon90 == 0:
        defcon90 = (
            _f(p, "clearances_blocks_interceptions_per_90")
            + _f(p, "tackles_per_90")
            + _f(p, "recoveries_per_90")
        )
    dc_target = prior["defcon_p90"] if prior else DEFCON_PRIOR_P90[pos]
    defcon90 = _shrink(min(defcon90, 60.0), minutes, dc_target, params.shrink_minutes)
    thr = DEFCON_THRESHOLD.get(pos, 999)
    defcon_prob = max(0.0, min(0.9, (defcon90 / thr) ** 1.3)) if thr < 999 else 0.0
    defcon_component = defcon_prob * DEFCON_PTS

    saves_component = 0.0
    if pos == "GKP":
        sv_target = prior["saves_p90"] if prior else 3.0
        saves90 = _shrink(_f(p, "saves_per_90"), minutes, sv_target, params.shrink_minutes)
        saves_component = saves90 / 3.0  # 1 pt per 3 saves

    return cs_component + defcon_component + saves_component


def _bonus_p90(p: dict, params: Params, prior: dict | None) -> float:
    """Bonus-points rate — this season's, shrunk toward last season's."""
    mins = _f(p, "minutes")
    prior_bonus = prior["bonus_p90"] if prior else 0.15
    if mins < 90:
        raw = max(0.0, (_f(p, "points_per_game") - 2.0) * 0.12)
    else:
        raw = 0.6 * (_f(p, "bonus") * 90.0 / mins) + 0.02 * (_f(p, "ict_index") * 90.0 / mins)
    return _shrink(raw, mins, prior_bonus, params.shrink_minutes) * params.bonus_scale


def _discipline_p90(p: dict) -> float:
    mins = _f(p, "minutes") or 1.0
    yc = _f(p, "yellow_cards") * 90.0 / mins
    rc = _f(p, "red_cards") * 90.0 / mins
    return yc * 1.0 + rc * 3.0


# --------------------------------------------------------------------------- #
# Assembly                                                                    #
# --------------------------------------------------------------------------- #

def project(cfg: dict | None = None, *, horizon_events: list[int] | None = None) -> list[PlayerXP]:
    """Return a PlayerXP for every non-removed player, sorted by weighted xP desc."""
    params = Params.from_config(cfg or {})
    boot = fpl_api.bootstrap_static()

    if horizon_events is None:
        nxt = fpl_api.next_event()["id"]
        horizon_events = list(range(nxt, nxt + params.horizon))
    fx_map = team_fixtures_by_event(horizon_events)

    finished = sum(1 for e in boot["events"] if e["finished"])
    ratings = team_ratings(boot, fpl_api.fixtures())
    recent = recent_histories(boot["elements"]) if finished else {}

    out: list[PlayerXP] = []
    for p in boot["elements"]:
        if p.get("removed"):
            continue
        pos = fpl_api.POS_BY_TYPE[p["element_type"]]
        prior = priors.prior_for(p.get("code"))
        start_prob, min_notes = _start_probability(p, finished, params, prior,
                                                   recent.get(p["id"]))

        att90, att_notes = _attacking_p90(p, pos, params, prior)
        bonus90 = _bonus_p90(p, params, prior)
        disc90 = _discipline_p90(p)

        per_gw: list[float] = []
        for ev in horizon_events:
            fixtures_this_ev = fx_map.get(ev, {}).get(p["team"], [])
            gw_xp = 0.0
            for fx in fixtures_this_ev:
                my_att, my_def = ratings[p["team"]]
                op_att, op_def = ratings[fx["opponent"]]
                lam_for = _expected_goals(my_att, op_def, fx["home"], params)
                lam_against = _expected_goals(op_att, my_def, not fx["home"], params)
                # A player's per-90 rates already reflect his team's average
                # fixture; scale by how this one compares.
                att_mult = (lam_for / my_att) ** params.fixture_swing
                def90 = _defensive_p90(p, pos, lam_against, params, prior)

                minutes_factor = start_prob * 0.9 + 0.1 * (start_prob ** 0.5)
                appearance = start_prob * 1.0 + start_prob * 0.82  # ~P(60+) ≈ 0.82·P(start)
                fx_xp = (
                    appearance
                    + minutes_factor * (att90 * att_mult + def90 + bonus90)
                    - minutes_factor * disc90
                )
                gw_xp += max(0.0, min(params.max_fixture_xp, fx_xp))
            per_gw.append(round(gw_xp, 3))

        # Blend every GW toward FPL's own ep_next (≈ recent form) at a flat,
        # low weight. No extra weight for thin samples: after GW1 everyone has
        # ≤90 min, and leaning on one game's points drove a 7-transfer hit
        # spree in the GW1-5 backtest. Thin samples already shrink to priors.
        api_ep = _f(p, "ep_next")
        w0 = params.api_anchor_weight
        for i in range(len(per_gw)):
            # Anchor fades across the horizon (ep_next only really speaks to GW+1).
            w = w0 * (0.72 ** i)
            per_gw[i] = round((1 - w) * per_gw[i] + w * api_ep, 3)

        weighted = sum(v * (params.decay ** k) for k, v in enumerate(per_gw))
        out.append(
            PlayerXP(
                element=p["id"],
                name=p["web_name"],
                pos=pos,
                team=p["team"],
                cost=p["now_cost"],
                per_gw=per_gw,
                start_prob=round(start_prob, 3),
                weighted=round(weighted, 3),
                notes=min_notes + att_notes,
            )
        )

    out.sort(key=lambda x: x.weighted, reverse=True)
    return out


def index_by_element(projections: list[PlayerXP]) -> dict[int, PlayerXP]:
    return {pp.element: pp for pp in projections}
