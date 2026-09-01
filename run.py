"""FPL Optimizer Bot — pipeline entry point.

Steps (SPEC.md §"Architecture"):
  1. Pull FPL bootstrap-static + fixtures + (if a team exists) the live squad.
  2. Decide whether this run is inside the pre-deadline notification window.
  3. Build the expected-points projections.
  4. Optimise: initial squad OR multi-week transfer + chip plan; pick captain.
  5. Claude sanity-check pass over the plan + fresh news.
  6. If in the window, email the recommendation.
  7. Persist state.json.

Environment / flags:
  DRY_RUN=1            compute + print, never send email, never write state
  FORCE_NOTIFY=1       ignore the deadline window and act now (manual runs)
  NO_CLAUDE=1          skip the Claude review pass
  FPL_STATE=path       override state.json location
  GMAIL_APP_PASSWORD   consumed by emailer.py (GitHub secret)
  ANTHROPIC_API_KEY    enables the Claude review pass

The workflow also reads the line `RECHECK_IN_MINUTES=<n>` from stdout to decide
whether to schedule a tighter re-run near a deadline.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import yaml

from fplbot import fpl_api
from fplbot.captain import choose_captain
from fplbot.claude_review import review_plan
from fplbot.deadlines import notification_state, upcoming_deadlines
from fplbot.optimizer import build_initial_squad
from fplbot.predict import index_by_element, project
from fplbot.report import build_context, build_email
from fplbot.state import TeamState
from fplbot.strategy import profile_for_rank
from fplbot.transfers import evaluate_chip_options

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {}


def _recipients() -> list[str]:
    """notify.recipients from config.yaml, else emailer's built-in default."""
    try:
        rcpts = load_config().get("notify", {}).get("recipients") or []
    except Exception:  # noqa: BLE001 — config must never block an alert email
        rcpts = []
    if rcpts:
        return [str(r).strip() for r in rcpts if str(r).strip()]
    from emailer import DEFAULT_RECIPIENT
    return [DEFAULT_RECIPIENT]


def _send(subject: str, body: str) -> None:
    from emailer import send_email  # local import so DRY_RUN needs no creds
    send_email(subject, body, to=_recipients())


def main() -> int:
    dry = bool(os.environ.get("DRY_RUN"))
    force = bool(os.environ.get("FORCE_NOTIFY"))
    no_claude = bool(os.environ.get("NO_CLAUDE"))
    cfg = load_config()
    nc = cfg.get("notify", {})

    state_path = Path(os.environ.get("FPL_STATE", ROOT / "state.json"))
    state = TeamState.load(state_path)

    fpl_api.clear_cache()
    fpl_api.bootstrap_static()  # step 1

    # step 2 — deadline window
    ns = notification_state(
        notify_window_hours=nc.get("window_hours", 26.0),
        tighten_window_hours=nc.get("tighten_hours", 3.0),
    )
    print(f"[deadline] {ns.reason}")
    print(f"RECHECK_IN_MINUTES={ns.recheck_in_minutes}")
    if ns.deadline is None:
        print("[done] season over")
        return 0

    # At most two scheduled emails per gameweek: one when we first enter the
    # 26h window ("opening"), one when we first enter the final-hours window
    # ("final", which catches late team news via the Claude pass). A manual
    # dispatch (FORCE_NOTIFY) always sends and doesn't touch the tracking.
    ev = ns.deadline.event_id
    in_final = ns.hours_away is not None and ns.hours_away <= nc.get("tighten_hours", 3.0)
    if force:
        stage = "manual"
    elif ns.should_notify and state.notified_event != ev:
        stage = "opening"
    elif ns.should_notify and in_final and not state.notified_final:
        stage = "final"
    else:
        stage = None

    if stage is None:
        if ns.should_notify:
            print(f"[done] already emailed GW{ev} (opening"
                  f"{'+final' if state.notified_final else ''}) — nothing to send")
        else:
            print("[done] outside notification window — nothing to send")
        if not dry:
            state.save(state_path)
        return 0
    print(f"[notify] stage: {stage}")

    # step 1b — live squad
    synced = state.sync_from_api(event=ns.deadline.event_id) if state.entry_id else False
    mode = "transfer" if state.has_team else "initial"
    print(f"[mode] {mode}  (entry_id={state.entry_id}, synced={synced})")

    # step 3 — projections
    horizon = [d.event_id for d in upcoming_deadlines(
        cfg.get("predict", {}).get("horizon", 5))]
    projections = project(cfg, horizon_events=horizon)
    xp_idx = index_by_element(projections)

    profile = profile_for_rank(state.overall_rank, gameweek=ns.deadline.event_id)
    print(f"[strategy] {profile.label}")

    # step 4 — optimise
    initial_sol = None
    chip_opts: list = []
    if mode == "initial":
        opt_cfg = cfg.get("optimizer", {})
        budget = opt_cfg.get("budget", 1000)
        universe = projections
        if opt_cfg.get("initial_template", True):
            from fplbot.strategy import templatise
            universe = templatise(projections)
            print("[optimizer] template build (low-variance, ownership-weighted)")
        initial_sol = build_initial_squad(
            universe, budget=budget,
            bench_weight=cfg.get("predict", {}).get("bench_weight", 0.15),
        )
        from fplbot.transfers import TransferPlan
        plan = TransferPlan(horizon, [], 0.0, message="initial mode")
        squad_for_captain = initial_sol.squad
        if initial_sol.infeasible:
            print(f"[optimizer] INFEASIBLE: {initial_sol.message}")
    else:
        plan, chip_opts = evaluate_chip_options(
            state, projections, horizon, cfg,
            only_first_n=cfg.get("transfers", {}).get("chip_scan_gws", 3),
        )
        squad_for_captain = (
            plan.next_gw.starting_xi + plan.next_gw.bench_order if plan.next_gw
            else state.squad_ids
        )
        if plan.infeasible:
            print(f"[transfers] INFEASIBLE: {plan.message}")

    solver_cap = plan.next_gw.captain if (plan.per_gw and not plan.infeasible) else None
    captain = choose_captain(squad_for_captain, projections, profile, solver_captain=solver_cap)
    print(f"[captain] {captain.name} ({'template' if captain.is_template else 'differential'})")

    # step 5 — Claude review
    review = None
    if not no_claude:
        ctx = build_context(
            deadline=ns.deadline, profile=profile, plan=plan, captain=captain,
            chip_options=chip_opts, xp_idx=xp_idx, mode=mode,
        )
        review = review_plan(ctx, use_web_search=cfg.get("claude", {}).get("web_search", True))
        print(f"[claude] {review.verdict}: {review.summary[:120]}")

    # step 6 — email
    subject, body = build_email(
        mode=mode, deadline=ns.deadline, profile=profile, plan=plan, captain=captain,
        chip_options=chip_opts, xp_idx=xp_idx, review=review, initial_squad=initial_sol,
    )
    print("\n" + "=" * 60 + f"\nSUBJECT: {subject}\n" + "=" * 60)
    print(body)

    if dry:
        print("\n[DRY_RUN — email not sent, state not written]")
        return 0

    _send(subject, body)
    print("\n[email sent]")

    # step 7 — persist
    state.last_event_processed = ev
    if stage == "opening":
        state.notified_event, state.notified_final = ev, False
    elif stage == "final":
        state.notified_event, state.notified_final = ev, True
    state.log_run({
        "event": ev, "mode": mode, "stage": stage,
        "captain": captain.name, "hits": plan.next_gw.hits if plan.next_gw else 0,
        "claude_verdict": review.verdict if review else "skipped",
    })
    state.save(state_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — a crash must still alert
        err = traceback.format_exc()
        print(err, file=sys.stderr)
        if not os.environ.get("DRY_RUN"):
            try:
                _send("FPL bot FAILED", "The FPL optimizer run raised an error:\n\n" + err)
            except Exception:  # noqa: BLE001
                print("could not send failure email", file=sys.stderr)
        raise SystemExit(1)
