"""Rebuild data/priors.json from the FPL element-summary endpoint.

    python scripts/refresh_priors.py

Pulls last 1–2 seasons of per-player totals (`history_past`), converts them to
per-90 point rates, and writes data/priors.json keyed by element_code. Run it
every month or so — prior-season output doesn't change, but promoted-team and
new-signing histories get filled in as the API learns their codes.

A monthly GitHub workflow (.github/workflows/refresh_priors.yml) does this
automatically and commits the result.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplbot import fpl_api  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "priors.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
GOAL_PTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
ASSIST_PTS = 3
SEASON_WEIGHTS = [0.62, 0.38]   # most-recent season, then the one before


def _fetch(pid: int):
    for _ in range(3):
        try:
            req = urllib.request.Request(
                f"https://fantasy.premierleague.com/api/element-summary/{pid}/",
                headers={"User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                return pid, json.loads(resp.read())
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
    return pid, None


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _season_rates(row: dict, pos: str) -> dict:
    mins = _f(row.get("minutes"))
    if mins < 1:
        return {}
    xg90 = _f(row.get("expected_goals")) * 90 / mins
    xa90 = _f(row.get("expected_assists")) * 90 / mins
    # fall back to actual G/A when a season predates xG being recorded
    if xg90 == 0 and xa90 == 0:
        xg90 = _f(row.get("goals_scored")) * 90 / mins * 0.85
        xa90 = _f(row.get("assists")) * 90 / mins * 0.85
    return {
        "att_p90": xg90 * GOAL_PTS[pos] + xa90 * ASSIST_PTS,
        "defcon_p90": _f(row.get("defensive_contribution")) * 90 / mins,
        "saves_p90": _f(row.get("saves")) * 90 / mins,
        "bonus_p90": _f(row.get("bonus")) * 90 / mins,
        "start_rate": min(1.0, _f(row.get("starts")) / 38.0),
        "minutes": mins,
    }


def build(summaries: dict, pos_by_code: dict) -> dict:
    out: dict[str, dict] = {}
    for _pid, data in summaries.items():
        if not data:
            continue
        past = data.get("history_past") or []
        if not past:
            continue
        code = str(past[-1].get("element_code"))
        pos = pos_by_code.get(code)
        if not pos:
            continue
        seasons = list(reversed(past))[:2]           # newest first
        weighted: dict[str, float] = {}
        wsum = 0.0
        used = 0
        for w, srow in zip(SEASON_WEIGHTS, seasons):
            r = _season_rates(srow, pos)
            if not r:
                continue
            eff = w * (r["minutes"] ** 0.5)          # also weight by sample size
            for k, v in r.items():
                if k == "minutes":
                    continue
                weighted[k] = weighted.get(k, 0.0) + eff * v
            wsum += eff
            used += 1
        if wsum == 0:
            continue
        rec = {k: round(v / wsum, 4) for k, v in weighted.items()}
        rec["minutes"] = int(sum(_f(s.get("minutes")) for s in seasons))
        rec["seasons"] = used
        out[code] = rec
    return out


def main() -> int:
    boot = fpl_api.bootstrap_static()
    pos_by_code = {str(p["code"]): fpl_api.POS_BY_TYPE[p["element_type"]]
                   for p in boot["elements"]}
    ids = [p["id"] for p in boot["elements"]]

    print(f"fetching element-summary for {len(ids)} players...")
    t0 = time.time()
    summaries: dict[int, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for pid, data in ex.map(_fetch, ids):
            if data:
                summaries[pid] = data
    print(f"  {len(summaries)}/{len(ids)} in {time.time() - t0:.0f}s")

    priors = build(summaries, pos_by_code)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(priors, indent=0, sort_keys=True) + "\n")
    print(f"wrote {OUT}  ({len(priors)} players with a trusted prior)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
