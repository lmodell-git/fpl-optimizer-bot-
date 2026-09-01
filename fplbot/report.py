"""Turn a recommendation into an email (subject, body) and a Claude context dict."""

from __future__ import annotations

from datetime import datetime, timezone

from . import fpl_api, pricing
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
        "price_moves": _price_context(plan),
    }


def _price_context(plan: TransferPlan) -> dict:
    pl = fpl_api.players_by_id()
    nxt = plan.next_gw
    if nxt is None:
        return {}
    targets = {}
    for e in nxt.transfers_in:
        s = pricing.price_signal(pl.get(e, {}))
        if s:
            targets[_name(e)] = s["note"]
    squad = {}
    for e in nxt.starting_xi + nxt.bench_order:
        s = pricing.price_signal(pl.get(e, {}))
        if s and s["direction"] == "fall":
            squad[_name(e)] = s["note"]
    return {"transfer_targets": targets, "squad_price_falls": squad}


# --------------------------------------------------------------------------- #
# The email                                                                   #
# --------------------------------------------------------------------------- #

def _gw_block(g: GWPlan, xp_idx) -> str:
    lines = [f"GW{g.event}"]
    if g.chip:
        lines.append(f"  CHIP: {g.chip.upper()}")
    if g.transfers_in or g.transfers_out:
        outs = ", ".join(_fmt_player(e, xp_idx) for e in g.transfers_out) or "—"
        ins = ", ".join(_fmt_player(e, xp_idx) for e in g.transfers_in) or "—"
        tag = f"  ({g.hits} hit{'s' if g.hits != 1 else ''}, -{g.hits * 4} pts)" if g.hits else ""
        lines.append(f"  OUT: {outs}")
        lines.append(f"  IN : {ins}{tag}")
    else:
        lines.append(f"  No transfer (roll — {g.free_transfers_before} FT banked)")
    lines.append(f"  C: {_name(g.captain)}   VC: {_name(g.vice_captain)}")
    return "\n".join(lines)


def _action_line(plan: TransferPlan, deadline: Deadline) -> str:
    """One-line 'what do I do before this deadline' headline."""
    nxt = plan.next_gw
    if plan.infeasible or nxt is None:
        return "ACTION: no plan produced — see notes below"
    pl = fpl_api.players_by_id()

    if nxt.chip:
        base = f"play {nxt.chip.upper()}"
        if nxt.transfers_in:
            n = len(nxt.transfers_in)
            base += f" + {n} transfer{'s' if n != 1 else ''}"
    elif nxt.transfers_in:
        n = len(nxt.transfers_in)
        base = f"make {n} transfer{'s' if n != 1 else ''} now"
        base += (f" — takes a {nxt.hits}×−4 hit (−{nxt.hits * 4} pts)"
                 if nxt.hits else " (free)")
    else:
        follow = next((g for g in plan.per_gw[1:] if g.transfers_in), None)
        if follow:
            names = ", ".join(_name(e) for e in follow.transfers_in)
            base = (f"roll this week — {nxt.free_transfers_before} FT banked; "
                    f"next move planned for GW{follow.event} ({names})")
        else:
            base = (f"roll — nothing rated worth doing in the next "
                    f"{len(plan.per_gw)} GWs")

    urgent = [f"buy {_name(e)} before its price rise"
              for e in nxt.transfers_in if pricing.urgent_buy(pl.get(e, {}))]
    urgent += [f"move {_name(e)} out before its price drop"
               for e in nxt.transfers_out if pricing.urgent_sell(pl.get(e, {}))]
    tail = f"  ⏰ {'; '.join(urgent)}" if urgent else ""
    return f"ACTION: {base}.{tail}"


def _price_section(plan: TransferPlan, squad_ids: list[int]) -> list[str]:
    pl = fpl_api.players_by_id()
    nxt = plan.next_gw
    targets = set(nxt.transfers_in) if nxt else set()

    rising = []
    for e in targets:
        s = pricing.price_signal(pl.get(e, {}))
        if s and s["direction"] == "rise":
            rising.append(f"  ↑ target  {_name(e)} — {s['note']}")
    falling = []
    for e in squad_ids:
        if e in targets:
            continue
        s = pricing.price_signal(pl.get(e, {}))
        if s and s["direction"] == "fall":
            falling.append(f"  ↓ squad   {_name(e)} — {s['note']}")

    if not (rising or falling):
        return []
    return ["PRICE MOVES", "-" * 40, *rising, *falling,
            "  (buys use current price; a rise before you act costs you the 0.1m)", ""]


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
    if nxt and nxt.transfers_in:
        head = f"{len(nxt.transfers_in)} transfer" + ("s" if len(nxt.transfers_in) != 1 else "")
        if nxt.hits:
            head += f" ({nxt.hits}×-4)"
    else:
        head = "ROLL"
    pl = fpl_api.players_by_id()
    urgent = nxt and (any(pricing.urgent_buy(pl.get(e, {})) for e in nxt.transfers_in)
                      or any(pricing.urgent_sell(pl.get(e, {})) for e in nxt.transfers_out))
    cap_tag = f"C {captain.name}" + ("" if captain.is_template else " [diff]")
    chip_tag = f" · {nxt.chip.upper()}" if nxt and nxt.chip else ""
    price_tag = " · ⏰ price" if urgent else ""
    subject = f"FPL GW{gw} — {head} · {cap_tag}{chip_tag}{price_tag} (deadline {hrs:.0f}h)"

    lines: list[str] = []
    lines.append(f"FPL OPTIMIZER — GW{gw} recommendation")
    lines.append(f"Deadline: {deadline.deadline:%a %d %b %H:%M UTC}  ({hrs:.1f}h away)")
    lines.append(f"Rank profile: {profile.label}")
    lines.append("")
    lines.append(_action_line(plan, deadline))
    lines.append("")

    squad_ids = (plan.next_gw.starting_xi + plan.next_gw.bench_order
                 if plan.next_gw else [])
    lines += _price_section(plan, squad_ids)

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
    # sol.expected_points is in the optimiser's own objective units (which are
    # ownership-weighted in template mode) — recompute a real next-GW figure.
    real_next = sum(xp_idx[e].next_gw for e in sol.starting_xi if e in xp_idx)
    real_next += xp_idx.get(sol.captain).next_gw if sol.captain in xp_idx else 0.0
    lines = [
        f"FPL OPTIMIZER — initial squad for GW{deadline.event_id}",
        f"Deadline: {deadline.deadline:%a %d %b %H:%M UTC}",
        f"Spend: £{sol.spend / 10:.1f}m / £100.0m   Formation: {sol.formation}",
        f"Projected XI points next GW (incl. captain): {real_next:.1f}",
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
