"""Backtest the prediction approach against actual gameweek points.

The live model needs a full bootstrap-static snapshot per past gameweek, which
the API doesn't archive. So this rebuilds an *as-of* projection for each finished
GW from what IS retrievable per player — `element-summary`:
  * history_past  -> the same per-90 prior predict.py uses
  * history[<g]   -> minutes / xG / xA accumulated before GW g
  * history[g]    -> the fixture that was played (opponent, home) + ACTUAL points

It mirrors predict.py's shape (P(start) x per-90 rates x fixture), not its exact
code, so treat the numbers as directional — especially early season when only a
couple of gameweeks exist. Metrics firm up as the season runs.
"""

from __future__ import annotations

import concurrent.futures as _cf
import statistics
from dataclasses import dataclass, field

from . import fpl_api, priors


def _fetch_summaries(ids: list[int]) -> dict[int, dict]:
    """Concurrent element-summary fetch — 600 sequential GETs would be minutes."""
    out: dict[int, dict] = {}
    with _cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fpl_api.element_summary, i): i for i in ids}
        for f in _cf.as_completed(futs):
            try:
                out[futs[f]] = f.result()
            except Exception:  # noqa: BLE001
                pass
    return out

GOAL_PTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
ASSIST_PTS = 3
CS_PTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


@dataclass
class GWResult:
    gw: int
    n: int
    spearman: float
    mae: float
    bias: float                       # mean(pred - actual)
    top15_overlap: int
    pos_bias: dict = field(default_factory=dict)


@dataclass
class BacktestReport:
    per_gw: list[GWResult]
    overall_spearman: float
    overall_mae: float
    overall_bias: float
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        L = ["PREDICTION BACKTEST", "=" * 46,
             f"gameweeks scored: {[r.gw for r in self.per_gw]}",
             f"rank corr (Spearman):  {self.overall_spearman:+.3f}   "
             "(1 = perfect ranking, 0 = noise)",
             f"mean abs error:        {self.overall_mae:.2f} pts/player",
             f"bias (pred - actual):  {self.overall_bias:+.2f} pts/player", ""]
        for r in self.per_gw:
            L.append(f"GW{r.gw}: n={r.n:<3} corr {r.spearman:+.3f}  MAE {r.mae:.2f}  "
                     f"bias {r.bias:+.2f}  top-15 hit {r.top15_overlap}/15")
            pb = "  ".join(f"{k}:{v:+.1f}" for k, v in r.pos_bias.items())
            L.append(f"      position bias  {pb}")
        if self.notes:
            L += ["", *[f"note: {n}" for n in self.notes]]
        return "\n".join(L)


def _asof_xp(pos: str, prior: dict | None, before_rows: list[dict], fx_row: dict) -> float:
    """Rebuild an as-of projection for one player / one gameweek."""
    mins = sum(_f(r.get("minutes")) for r in before_rows)
    games = len(before_rows)

    # start probability
    prate = prior["start_rate"] if prior else 0.6
    if games:
        this_rate = sum(1 for r in before_rows if _f(r.get("minutes")) >= 60) / games
        w = min(1.0, games / 6.0)
        start_p = w * this_rate + (1 - w) * prate
    else:
        start_p = prate
    start_p = max(0.0, min(1.0, 0.3 + 0.7 * start_p))

    # attacking per-90: this-season-so-far shrunk toward the prior
    if mins > 0:
        xg90 = sum(_f(r.get("expected_goals")) for r in before_rows) * 90 / mins
        xa90 = sum(_f(r.get("expected_assists")) for r in before_rows) * 90 / mins
        raw_att = xg90 * GOAL_PTS[pos] + xa90 * ASSIST_PTS
    else:
        raw_att = None
    prior_att = prior["att_p90"] if prior else {"GKP": .05, "DEF": .55, "MID": 1.45, "FWD": 2.35}[pos]
    k = 300.0
    att90 = prior_att if raw_att is None else (
        (mins / (mins + k)) * raw_att + (k / (mins + k)) * prior_att)

    # crude fixture + clean-sheet handling from the row that was played
    home = fx_row.get("was_home", True)
    cs_prob = 0.30 * (1.1 if home else 0.9)
    def90 = cs_prob * CS_PTS[pos]
    if prior and prior.get("defcon_p90", 0) >= ({"DEF": 10, "MID": 12}.get(pos, 99)):
        def90 += 1.4  # likely to hit the defensive-contribution bonus

    appearance = start_p * 1.82
    minutes_factor = start_p
    return appearance + minutes_factor * (att90 * (1.06 if home else 0.94) + def90 + 0.4)


def run(finished_gws: list[int] | None = None) -> BacktestReport:
    boot = fpl_api.bootstrap_static()
    pos_by_id = {p["id"]: fpl_api.POS_BY_TYPE[p["element_type"]] for p in boot["elements"]}
    code_by_id = {p["id"]: p.get("code") for p in boot["elements"]}
    if finished_gws is None:
        finished_gws = [e["id"] for e in boot["events"] if e["finished"]]
    if not finished_gws:
        return BacktestReport([], float("nan"), float("nan"), float("nan"),
                              ["no finished gameweeks yet — nothing to score"])

    summaries = _fetch_summaries(list(pos_by_id))

    per_gw: list[GWResult] = []
    all_pred: list[float] = []
    all_act: list[float] = []

    for g in sorted(finished_gws):
        preds, acts, poss = [], [], []
        for pid, pos in pos_by_id.items():
            summ = summaries.get(pid)
            if not summ:
                continue
            rows = {r["round"]: r for r in summ.get("history", [])}
            if g not in rows or _f(rows[g].get("minutes")) < 1:
                continue                      # didn't feature — not a prediction miss
            before = [rows[r] for r in sorted(rows) if r < g]
            prior = priors.prior_for(code_by_id.get(pid))
            xp = _asof_xp(pos, prior, before, rows[g])
            preds.append(xp)
            acts.append(_f(rows[g].get("total_points")))
            poss.append(pos)

        if len(preds) < 5:
            continue
        pred_top = set(sorted(range(len(preds)), key=lambda i: -preds[i])[:15])
        act_top = set(sorted(range(len(acts)), key=lambda i: -acts[i])[:15])
        overlap = len(pred_top & act_top)
        errs = [abs(p - a) for p, a in zip(preds, acts)]
        pb = {}
        for pp in ("GKP", "DEF", "MID", "FWD"):
            d = [pr - ac for pr, ac, po in zip(preds, acts, poss) if po == pp]
            if d:
                pb[pp] = statistics.mean(d)
        per_gw.append(GWResult(
            gw=g, n=len(preds), spearman=_spearman(preds, acts),
            mae=statistics.mean(errs),
            bias=statistics.mean(p - a for p, a in zip(preds, acts)),
            top15_overlap=overlap, pos_bias=pb,
        ))
        all_pred += preds
        all_act += acts

    notes = []
    if len(finished_gws) < 4:
        notes.append(f"only {len(finished_gws)} GW(s) of data — results are directional, "
                     "re-run weekly")
    return BacktestReport(
        per_gw=per_gw,
        overall_spearman=_spearman(all_pred, all_act) if all_pred else float("nan"),
        overall_mae=statistics.mean(abs(p - a) for p, a in zip(all_pred, all_act)) if all_pred else float("nan"),
        overall_bias=statistics.mean(p - a for p, a in zip(all_pred, all_act)) if all_pred else float("nan"),
        notes=notes,
    )
