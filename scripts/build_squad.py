"""Build an initial 15-man squad on demand and print it.

    python scripts/build_squad.py                 # xP-optimal
    python scripts/build_squad.py --template      # low-variance, template-owned
    python scripts/build_squad.py --template --lock 355 --exclude 12
    python scripts/build_squad.py --budget 1000 --horizon 5

--template biases the objective hard toward effective ownership (what the top
managers actually own) with xP only as a tiebreak, and drops anyone below a
60% start probability. That's the "safe" build: it tracks the field instead of
trying to beat it (SPEC.md §"Strategy").

Element IDs for --lock / --exclude are printed next to every player.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplbot import fpl_api  # noqa: E402
from fplbot.deadlines import next_deadline, upcoming_deadlines  # noqa: E402
from fplbot.optimizer import build_initial_squad  # noqa: E402
from fplbot.predict import index_by_element, project  # noqa: E402
from fplbot.strategy import templatise  # noqa: E402

ORDER = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}


def _own(pl, e: int) -> float:
    try:
        return float(pl[e]["selected_by_percent"] or 0.0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", action="store_true", help="low-variance template build")
    ap.add_argument("--budget", type=int, default=1000, help="tenths of £m (1000 = £100.0m)")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--lock", type=int, nargs="*", default=[], help="element ids to force in")
    ap.add_argument("--exclude", type=int, nargs="*", default=[], help="element ids to bar")
    ap.add_argument("--min-start-prob", type=float, default=0.6,
                    help="template mode: drop players below this start probability")
    args = ap.parse_args()

    pl, tm = fpl_api.players_by_id(), fpl_api.teams_by_id()
    dl = next_deadline()
    horizon = [d.event_id for d in upcoming_deadlines(args.horizon)]
    projections = project({}, horizon_events=horizon)
    xp0 = index_by_element(projections)

    universe = (templatise(projections, min_start_prob=args.min_start_prob)
                if args.template else projections)
    sol = build_initial_squad(universe, budget=args.budget,
                              locked_in=args.lock, excluded=args.exclude)
    if sol.infeasible:
        print(f"INFEASIBLE: {sol.message}")
        return 1

    if args.template:
        cap = max(sol.starting_xi, key=lambda e: (_own(pl, e), xp0[e].next_gw))
    else:
        cap = sol.captain
    vc = sorted((e for e in sol.starting_xi if e != cap),
                key=lambda e: (_own(pl, e), xp0[e].next_gw))[-1]

    kind = "TEMPLATE / SAFE" if args.template else "xP-OPTIMAL"
    print(f"{kind} INITIAL SQUAD — GW{dl.event_id}, deadline {dl.deadline:%a %d %b %H:%M UTC}")
    print(f"Spend £{sol.spend / 10:.1f}m / £{args.budget / 10:.1f}m   "
          f"bank £{(args.budget - sol.spend) / 10:.1f}m   Formation {sol.formation}\n")

    print(f"{'':2}{'id':>4}  {'player':<16}{'club':<5}{'pos':<4}{'£':>6}  {'own%':>6}  {'xP':>6}")
    for e in sorted(sol.starting_xi, key=lambda e: (ORDER[xp0[e].pos], -_own(pl, e))):
        p = pl[e]
        tag = " (C)" if e == cap else (" (VC)" if e == vc else "")
        print(f"  {e:>4}  {p['web_name']:<16}{tm[p['team']]['short_name']:<5}"
              f"{fpl_api.POS_BY_TYPE[p['element_type']]:<4}£{p['now_cost'] / 10:>4.1f}  "
              f"{_own(pl, e):>5.1f}%  {xp0[e].next_gw:>6.2f}{tag}")
    print("  bench:")
    for i, e in enumerate(sol.bench_order, 1):
        p = pl[e]
        print(f"  {e:>4}  [{i}] {p['web_name']:<12}{tm[p['team']]['short_name']:<5}"
              f"{fpl_api.POS_BY_TYPE[p['element_type']]:<4}£{p['now_cost'] / 10:>4.1f}  {_own(pl, e):>5.1f}%")

    print("\nClub counts:", dict(Counter(tm[pl[e]["team"]]["short_name"] for e in sol.squad)))
    print("Element IDs:", sol.squad)
    print(f"XI template ownership sum: {sum(_own(pl, e) for e in sol.starting_xi):.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
