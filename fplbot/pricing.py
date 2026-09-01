"""Price-change signals.

FPL players rise/fall in price as net transfers accumulate. The API exposes:
  * price_change_percent      — progress toward the next change; ±100 triggers it
  * price_change_projections  — [{offset_days, projected_percent, likelihood}]
                                likelihood: +3/+5 = rise likely/imminent,
                                            -3/-5 = fall likely/imminent, ±1 = none
  * price_change_hourly_rate  — current momentum
  * cost_change_event         — change already banked this gameweek (tenths of £m)

`price_signal()` turns that into a plain-English "buy before tonight" / "sell
before it drops" flag for the email and the Claude context.
"""

from __future__ import annotations


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def price_signal(element: dict) -> dict | None:
    """{'direction': 'rise'|'fall', 'days': int|None, 'imminent': bool, 'note': str}
    or None when there's no meaningful pressure."""
    pct = _f(element.get("price_change_percent"))
    projs = element.get("price_change_projections") or []
    changed = int(element.get("cost_change_event") or 0)

    for pr in sorted(projs, key=lambda x: x.get("offset", 99)):
        pp = _f(pr.get("projected_percent"))
        lk = pr.get("likelihood", 0) or 0
        off = int(pr.get("offset", 0) or 0)
        if pp >= 100 and lk >= 3:
            return {"direction": "rise", "days": off, "imminent": off <= 0,
                    "note": _phrase("rise", off, changed)}
        if pp <= -100 and lk <= -3:
            return {"direction": "fall", "days": off, "imminent": off <= 0,
                    "note": _phrase("fall", off, changed)}

    if pct >= 65 and any((p.get("likelihood") or 0) >= 3 for p in projs):
        return {"direction": "rise", "days": None, "imminent": False,
                "note": f"rise building — {pct:.0f}% of the way there"}
    if pct <= -65 and any((p.get("likelihood") or 0) <= -3 for p in projs):
        return {"direction": "fall", "days": None, "imminent": False,
                "note": f"fall building — {abs(pct):.0f}% of the way there"}
    if changed:
        return {"direction": "rise" if changed > 0 else "fall", "days": 0, "imminent": False,
                "note": f"already {'+' if changed > 0 else '-'}£{abs(changed) / 10:.1f}m this GW"}
    return None


def _phrase(direction: str, off: int, changed: int) -> str:
    tail = ""
    if changed:
        tail = f"; already {'+' if changed > 0 else '-'}£{abs(changed) / 10:.1f}m this GW"
    when = "today" if off <= 0 else ("~tomorrow" if off == 1 else f"in ~{off} days")
    return f"{direction} projected {when}{tail}"


def urgent_buy(element: dict) -> bool:
    s = price_signal(element)
    return bool(s and s["direction"] == "rise" and s["days"] is not None and s["days"] <= 1)


def urgent_sell(element: dict) -> bool:
    s = price_signal(element)
    return bool(s and s["direction"] == "fall" and s["days"] is not None and s["days"] <= 1)
