"""Attempt an auto-execution end to end — DRY-RUN by default.

    FPL_EMAIL=... FPL_PASSWORD=... python scripts/try_execute.py
    ... python scripts/try_execute.py --live      # actually POST (guardrails still apply)

What it does: build the current recommended transfer + lineup plan, log in to the
FPL account, fetch the live team, construct the exact payloads, and print them.
With no --live flag (or if any guardrail fails) it POSTs nothing.

Requires: state.json has a real `entry_id`, and FPL_EMAIL / FPL_PASSWORD in the
environment. On GitHub those are repo secrets — run this via the
`fpl-execute-dryrun` workflow (Actions tab) where the secrets exist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from fplbot import fpl_api  # noqa: E402
from fplbot.captain import choose_captain  # noqa: E402
from fplbot.claude_review import Review  # noqa: E402
from fplbot.deadlines import next_deadline, upcoming_deadlines  # noqa: E402
from fplbot.execute import ExecuteError, run_execution  # noqa: E402
from fplbot.predict import index_by_element, project  # noqa: E402
from fplbot.state import TeamState  # noqa: E402
from fplbot.strategy import profile_for_rank  # noqa: E402
from fplbot.transfers import recommend_transfers  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="allow a real POST if every guardrail passes (default: dry-run)")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    state = TeamState.load()
    if state.entry_id is None:
        print("state.json has no entry_id — create the FPL team and set it first.")
        return 2
    state.sync_from_api()

    dl = next_deadline()
    horizon = [d.event_id for d in upcoming_deadlines(cfg.get("predict", {}).get("horizon", 5))]
    proj = project(cfg, horizon_events=horizon)
    idx = index_by_element(proj)
    plan = recommend_transfers(state, proj, horizon, cfg)
    if plan.infeasible:
        print(f"no plan: {plan.message}")
        return 2

    prof = profile_for_rank(state.overall_rank, gameweek=dl.event_id)
    squad = plan.next_gw.starting_xi + plan.next_gw.bench_order
    cap = choose_captain(squad, proj, prof, solver_captain=plan.next_gw.captain)

    # try_execute doesn't run the Claude pass — treat as "no verdict" so the
    # verdict guardrail blocks a live submit unless you've relaxed it in config.
    review = Review(True, "unavailable", "try_execute: Claude pass not run", [], [])

    try:
        res = run_execution(state, plan, cap, cfg, review,
                            deadline_hours=dl.hours_away(), force_dry=not args.live)
    except ExecuteError as exc:
        print(f"\nEXECUTE FAILED: {exc}")
        return 1

    print("\n".join(res.log))
    print(f"\nsubmitted={res.submitted}  dry_run={res.dry_run}")
    if res.reason_forced_dry:
        print(f"(forced dry-run: {res.reason_forced_dry})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
