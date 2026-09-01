"""Turn a recommendation into an email (subject, body) and a Claude context dict."""

from __future__ import annotations

from datetime import datetime, timezone

from . import fpl_api
from .captain import CaptainPick
from .deadlines import Deadline
from .predict import PlayerXP
from .strategy import RiskProfile
from .transfers import ChipOption, GWPlan, TransferPlan


def _name(el: int) -> str:
    return fpl_api.players_by_id().get(el, {}).get("web_name", f"#{el}")


def _team(el: int) -> str:
    p = fpl_api.players_by_id().get(el, {})
    return fpl_api.teams_by_id().get(p.get("team"), {}).get("short_name", "?")


def _pos(el: int) -> str:
    p = fpl_api.players_by_id().get(el, {})
    return fpl_api.POS_BY_TYPE.get(p.get("element_type"), "?")


def _price(el: int) -> str:
    return f"£{fpl_api.players_by_id().get(el, {}).get('now_cost', 0) / 10:.1f}"


def _fmt_player(el: int, xp_idx: dict[int, PlayerXP] | None = None) -> str:
    s = f"{_name(el)} ({_team(el)}, {_pos(el)}, {_price(el)})"
    if xp_idx and el in xp_idx:
        s += f"  xP≈{xp_idx[el].next_gw:.2f}"
    return s


# --------------------------------------------------------------------------- #
# Context for the Claude review                                               #
# --------------------------------------------------------------------------- #

def build_context(
    *,
    deadline: Deadline,
    profile: RiskProfile,
    plan: TransferPlan,
    captain: CaptainPick,
    chip_options: list[ChipOption],
    xp_idx: dict[int, PlayerXP],
    mode: str,
) -> dict:
    nxt = plan.next_gw
    news = {}
    watch = []
    for el, p in fpl_api.players_by_id().items():
        if p.get("news") and (p.get("chance_of_playing_next_round") not in (100, None)):
            watch.append({
                "player": p["web_name"], "team": _team(el),
                "news": p["news"], "chance": p.get("chance_of_playing_next_round"),
            })
    relevant_ids = set(nxt.starting_xi + nxt.transfers_in + nxt.bench_order) if nxt else set()
    for el in relevant_ids:
        p = fpl_api.players_by_id().get(el, {})
        if p.get("news"):
            news[p.get("web_name", el)] = p["news"]

    return {
        "mode": mode,
        "gameweek": deadline.event_id,
        "deadline_utc": deadline.deadline.isoformat(),
        "rank_profile": {
            "rank": profile.rank, "label": profile.label,
            "differential_appetite": profile.differential_appetite,
            "captain_diff_threshold": profile.captain_diff_threshold,
            "max_hit_chase": profile.max_hit_chase,
        },
        "recommended_next_gw": None if not nxt else {
            "transfers_in": [_fmt_player(e, xp_idx) for e in nxt.transfers_in],
            "transfers_out": [_fmt_player(e, xp_idx) for e in nxt.transfers_out],
            "hits": nxt.hits,
            "free_transfers_before": nxt.free_transfers_before,
            "starting_xi": [_fmt_player(e, xp_idx) for e in nxt.starting_xi],
            "bench": [_fmt_player(e, xp_idx) for e in nxt.bench_order],
            "chip": nxt.chip,
            "expected_points": nxt.expected_points,
        },
        "captain": {
            "pick": captain.name, "is_template": captain.is_template,
            "template": captain.template_name, "xp_gap_vs_template": captain.xp_gap,
            "pick_ownership": captain.pick_ownership,
            "template_ownership": captain.template_ownership,
            "rationale": captain.rationale,
        },
        "multi_week_plan": [
            {
                "gw": g.event, "in": [_name(e) for e in g.transfers_in],
                "out": [_name(e) for e in g.transfers_out], "hits": g.hits,
                "captain": _name(g.captain), "chip": g.chip,
            }
            for g in plan.per_gw
        ],
        "chip_options_ranked": [
            {"chip": o.chip, "gw": o.event, "delta_vs_baseline": o.delta_vs_baseline}
            for o in chip_options[:5]
        ],
        "news_on_recommended_players": news,
        "injury_watch_list": watch[:25],
    }


# --------------------------------------------------------------------------- #
# The email                                                                   #
# --------------------------------------------------------------------------- #

def _gw_block(g: GWPlan, xp_idx) -> str:
    lines = [f"GW{g.event}"]
    if g.chip:
        lines.append(f"  CHIP: {g.chip.upper()}")
    if g.transfers_in or g.transfers_out:
        for o, i in zip(g.transfers_out + [None] * len(g.transfers_in),
                        [None] * len(g.transfers_out) + g.transfers_in):
            pass
        outs = ", ".join(_fmt_player(e, xp_idx) for e in g.transfers_out) or "—"
        ins = ", ".join(_fmt_player(e, xp_idx) for e in g.transfers_in) or "—"
        tag = f"  ({g.hits} hit{'s' if g.hits != 1 else ''}, -{g.hits * 4} pts)" if g.hits else ""
        lines.append(f"  OUT: {outs}")
        lines.append(f"  IN : {ins}{tag}")
    else:
        lines.append(f"  No transfer (roll — {g.free_transfers_before} FT banked)")
    lines.append(f"  C: {_name(g.captain)}   VC: {_name(g.vice_captain)}")
    return "\n".join(lines)


def build_email(
    *,
    mode: str,
    deadline: Deadline,
    profile: RiskProfile,
    plan: TransferPlan,
    captain: CaptainPick,
    chip_options: list[ChipOption],
    xp_idx: dict[int, PlayerXP],
    review=None,
    initial_squad=None,
) -> tuple[str, str]:
    hrs = deadline.hours_away()
    gw = deadline.event_id

    if mode == "initial" and initial_squad is not None:
        subject = f"FPL — initial squad for GW{gw} (deadline in {hrs:.0f}h)"
        body = _initial_body(deadline, profile, initial_squad, captain, chip_options, xp_idx, review)
        return subject, body

    nxt = plan.next_gw
    if nxt and (nxt.transfers_in or nxt.hits):
        head = f"{len(nxt.transfers_in)} transfer" + ("s" if len(nxt.transfers_in) != 1 else "")
        if nxt.hits:
            head += f" ({nxt.hits}×-4)"
    else:
        head = "roll transfer"
    cap_tag = f"C {captain.name}" + ("" if captain.is_template else " [diff]")
    chip_tag = f" · {nxt.chip.upper()}" if nxt and nxt.chip else ""
    subject = f"FPL GW{gw} — {head} · {cap_tag}{chip_tag} (deadline {hrs:.0f}h)"

    lines: list[str] = []
    lines.append(f"FPL OPTIMIZER — GW{gw} recommendation")
    lines.append(f"Deadline: {deadline.deadline:%a %d %b %H:%M UTC}  ({hrs:.1f}h away)")
    lines.append(f"Rank profile: {profile.label}")
    lines.append("")

    if review is not None and review.ok and review.verdict != "unavailable":
        lines.append(f"CLAUDE REVIEW — verdict: {review.verdict.upper()}")
        if review.summary:
            lines.append(review.summary)
        for c in review.concerns:
            lines.append(f"  ⚠ {c}")
        for c in review.suggested_changes:
            lines.append(f"  → {c}")
        lines.append("")
    elif review is not None and not review.ok:
        lines.append(f"[{review.summary}]")
        lines.append("")

    lines.append("THIS WEEK")
    lines.append("-" * 40)
    if plan.infeasible:
        lines.append(f"Solver could not produce a plan: {plan.message}")
    else:
        lines.append(_gw_block(nxt, xp_idx))
        lines.append("")
        lines.append(f"  Captain call: {captain.rationale}")
        lines.append("")
        lines.append("  Starting XI:")
        for e in nxt.starting_xi:
            lines.append(f"    {_fmt_player(e, xp_idx)}")
        lines.append("  Bench (in order):")
        for e in nxt.bench_order:
            lines.append(f"    {_fmt_player(e, xp_idx)}")

    lines.append("")
    lines.append(f"HORIZON PLAN (next {len(plan.per_gw)} GWs)")
    lines.append("-" * 40)
    for g in plan.per_gw:
        lines.append(_gw_block(g, xp_idx))
        lines.append("")

    if chip_options:
        lines.append("CHIP TIMING (ranked by horizon xP gain vs no-chip)")
        lines.append("-" * 40)
        for o in chip_options[:5]:
            flag = "  <-- worth planning" if o.delta_vs_baseline > 6 else ""
            lines.append(f"  {o.chip.upper():9s} GW{o.event}   {o.delta_vs_baseline:+.1f} pts{flag}")
        lines.append("")

    watch = [
        f"{p['web_name']} ({p['news']})"
        for p in fpl_api.players_by_id().values()
        if p.get("news") and p.get("chance_of_playing_next_round") not in (100, None)
    ]
    if watch:
        lines.append("WATCH LIST (flagged players, leaguewide)")
        lines.append("-" * 40)
        for w in watch[:15]:
            lines.append(f"  {w}")
        lines.append("")

    lines.append(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · recommend-only (no auto-execution)")
    return subject, "\n".join(lines)


def _initial_body(deadline, profile, sol, captain, chip_options, xp_idx, review) -> str:
    lines = [
        f"FPL OPTIMIZER — initial squad for GW{deadline.event_id}",
        f"Deadline: {deadline.deadline:%a %d %b %H:%M UTC}",
        f"Spend: £{sol.spend / 10:.1f}m / £100.0m   Formation: {sol.formation}",
        f"Projected XI points (weighted horizon): {sol.expected_points}",
        "",
    ]
    if review is not None and review.ok and review.summary:
        lines += [f"CLAUDE REVIEW — {review.verdict.upper()}", review.summary, ""]
    vc = None
    ranked = sorted(sol.starting_xi, key=lambda e: xp_idx[e].next_gw if e in xp_idx else 0,
                    reverse=True)
    for e in ranked:
        if e != captain.element:
            vc = e
            break
    lines.append("STARTING XI")
    lines.append("-" * 40)
    for e in sol.starting_xi:
        tag = "  (C)" if e == captain.element else ("  (VC)" if e == vc else "")
        lines.append(f"  {_fmt_player(e, xp_idx)}{tag}")
    lines.append("BENCH")
    lines.append("-" * 40)
    for e in sol.bench_order:
        lines.append(f"  {_fmt_player(e, xp_idx)}")
    lines += ["", f"Captain: {captain.rationale}", ""]
    lines.append("Once you've entered this team on the FPL site, paste the team ID into state.json.")
    return "\n".join(lines)
