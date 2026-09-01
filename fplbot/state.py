"""Persisted team state — the one thing GitHub Actions can't hold between runs.

Each Actions run is a fresh VM, so the squad, bank, free-transfer count, chips
used and a short history live in `state.json`, committed back to the repo at
the end of every run. Small, human-readable, versioned by git.

If `entry_id` is set and `sync_from_api` is called, the live FPL API is the
source of truth for the squad/bank each run and state.json just mirrors it
(plus purchase prices, which the picks endpoint gives us). Before the FPL team
exists, state.json is authoritative and starts empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from . import fpl_api

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "state.json"

# 2025/26 rules: up to 5 free transfers can be banked.
MAX_FREE_TRANSFERS = 5
ALL_CHIPS = ("wildcard", "freehit", "bboost", "3xc")  # wildcard usable twice/season


@dataclass
class Pick:
    element: int          # player id
    purchase_price: int   # now_cost (tenths of £m) when bought — for selling-price math
    selling_price: int    # what FPL would give you back today

    @staticmethod
    def from_api(d: dict) -> "Pick":
        return Pick(
            element=d["element"],
            purchase_price=d.get("purchase_price", d.get("selling_price", 0)),
            selling_price=d.get("selling_price", d.get("purchase_price", 0)),
        )


@dataclass
class TeamState:
    entry_id: int | None = None          # FPL team id; None until the team is created
    squad: list[Pick] = field(default_factory=list)   # 15 picks
    bank: int = 1000                     # tenths of £m; 1000 = £100.0m at season start
    free_transfers: int = 1
    chips_used: list[dict] = field(default_factory=list)   # [{"chip": "...", "event": N}]
    last_event_processed: int | None = None
    overall_rank: int | None = None
    history: list[dict] = field(default_factory=list)      # run log, newest last
    auto_execute: bool = False           # kept for the deferred write-path; do not flip on yet

    # ---- persistence ----------------------------------------------------- #

    @staticmethod
    def load(path: Path | str = DEFAULT_PATH) -> "TeamState":
        p = Path(path)
        if not p.exists():
            return TeamState()
        raw = json.loads(p.read_text())
        raw["squad"] = [Pick(**pk) for pk in raw.get("squad", [])]
        return TeamState(**raw)

    def save(self, path: Path | str = DEFAULT_PATH) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    # ---- helpers ------------------------------------------------------- #

    @property
    def has_team(self) -> bool:
        return self.entry_id is not None and len(self.squad) == 15

    @property
    def squad_ids(self) -> list[int]:
        return [pk.element for pk in self.squad]

    def chip_available(self, chip: str, *, wildcards_per_season: int = 2) -> bool:
        used = sum(1 for c in self.chips_used if c["chip"] == chip)
        if chip == "wildcard":
            return used < wildcards_per_season
        return used == 0

    def squad_value(self) -> int:
        """Selling value of the current squad (tenths of £m)."""
        return sum(pk.selling_price for pk in self.squad)

    def budget(self) -> int:
        """Total spend ceiling for a fresh optimisation: squad sell value + bank."""
        return self.squad_value() + self.bank

    def sync_from_api(self, event: int | None = None) -> bool:
        """Overwrite squad / bank / FT / rank / chips from the live API.

        Returns False (and changes nothing) if there's no entry_id yet.
        """
        if self.entry_id is None:
            return False
        ev = event or (fpl_api.current_event() or fpl_api.next_event())["id"]
        picks = fpl_api.entry_picks(self.entry_id, ev)
        hist = fpl_api.entry_history(self.entry_id)
        ent = fpl_api.entry(self.entry_id)

        self.squad = [Pick.from_api(p) for p in picks["picks"]]
        et = picks.get("entry_history", {})
        self.bank = et.get("bank", self.bank)
        self.free_transfers = _infer_free_transfers(hist, ev)
        self.chips_used = [
            {"chip": c["name"], "event": c["event"]} for c in hist.get("chips", [])
        ]
        self.overall_rank = ent.get("summary_overall_rank")
        return True

    def log_run(self, entry: dict) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self.history.append(entry)
        self.history = self.history[-50:]


def _infer_free_transfers(history: dict, upcoming_event: int) -> int:
    """Best-effort FT count from history: +1 per past GW, capped, minus transfers made.

    The API doesn't expose the banked FT count directly. This walks the per-GW
    `event_transfers` and reconstructs it under the current (max 5) rules.
    """
    ft = 1
    for row in sorted(history.get("current", []), key=lambda r: r["event"]):
        if row["event"] >= upcoming_event:
            break
        made = row.get("event_transfers", 0)
        ft = max(1, min(MAX_FREE_TRANSFERS, ft - made + 1))
    return ft
