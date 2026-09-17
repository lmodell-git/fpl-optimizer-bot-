"""State sync tests — run with: python -m pytest tests/ (or python tests/test_state.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplbot import fpl_api, state as st  # noqa: E402
from fplbot.state import Pick, TeamState, selling_price  # noqa: E402


def test_selling_price_half_profit_rule():
    assert selling_price(60, 60) == 60      # unchanged
    assert selling_price(60, 63) == 61      # +0.3 rise -> keep 0.1 (floor of half)
    assert selling_price(60, 64) == 62      # +0.4 rise -> keep 0.2
    assert selling_price(60, 58) == 58      # falls are yours in full
    assert selling_price(0, 55) == 55       # unknown purchase -> today's price


def test_public_picks_without_prices_keep_known_purchase(monkeypatch=None):
    fake_players = {1: {"now_cost": 63}, 2: {"now_cost": 50}, 3: {"now_cost": 45}}
    orig = fpl_api.players_by_id
    fpl_api.players_by_id = lambda: fake_players
    try:
        ts = TeamState(entry_id=1, squad=[Pick(1, 60, 60), Pick(9, 70, 70)])
        picks = ts._picks_with_prices([
            {"element": 1, "position": 1},        # still owned: purchase 60, now 63
            {"element": 2, "position": 2},        # new: bought at today's price
            {"element": 3, "position": 3, "purchase_price": 40, "selling_price": 42},
        ])
    finally:
        fpl_api.players_by_id = orig
    by = {p.element: p for p in picks}
    assert (by[1].purchase_price, by[1].selling_price) == (60, 61)
    assert (by[2].purchase_price, by[2].selling_price) == (50, 50)
    assert (by[3].purchase_price, by[3].selling_price) == (40, 42)  # API prices win
    assert all(p.selling_price > 0 for p in picks)


if __name__ == "__main__":
    test_selling_price_half_profit_rule()
    test_public_picks_without_prices_keep_known_purchase()
    print("ok")
