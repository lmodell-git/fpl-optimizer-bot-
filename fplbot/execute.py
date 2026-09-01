"""Auto-execution — logs into the FPL account and applies the recommended plan.

UNOFFICIAL AND FRAGILE. FPL has no public write API. This scripts the same
reverse-engineered flow the community `fpl` library uses:

    POST users.premierleague.com/accounts/login/   -> session cookies
    GET  fantasy.premierleague.com/api/my-team/{id}/  -> picks + selling prices
    POST fantasy.premierleague.com/api/transfers/     -> make transfers
    POST fantasy.premierleague.com/api/my-team/{id}/  -> set XI / captain / bench

It can break at any time (endpoint changes, Cloudflare, a new-device email
challenge). Every failure raises `ExecuteError`; the caller emails it and
flips `state.auto_execute` back to False so it never fails blind in a loop.

SAFETY — a live submit requires ALL of:
  * state.json  "auto_execute": true
  * config      execute.armed: true
  * config      execute.dry_run: false
  * the guardrail check passes (verdict, hit size, deadline freeze window)
Anything short of that forces dry-run: payloads are logged, nothing is POSTed.
Credentials come from the environment (FPL_EMAIL / FPL_PASSWORD); this module
never sees them written down and never logs them.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

LOGIN_URL = "https://users.premierleague.com/accounts/login/"
API = "https://fantasy.premierleague.com/api"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHIP_API_NAME = {"wildcard": "wildcard", "freehit": "freehit", "bboost": "bboost", "3xc": "3xc"}


class ExecuteError(RuntimeError):
    """Any failure in the login / fetch / submit flow."""


@dataclass
class ExecResult:
    dry_run: bool
    submitted: bool
    log: list[str] = field(default_factory=list)
    transfers_payload: dict | None = None
    lineup_payload: dict | None = None
    reason_forced_dry: str | None = None

    def line(self, msg: str) -> None:
        self.log.append(msg)


# --------------------------------------------------------------------------- #
# Session                                                                     #
# --------------------------------------------------------------------------- #

class FplSession:
    def __init__(self) -> None:
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    def _request(self, url: str, *, data: bytes | None = None, headers: dict | None = None,
                 method: str | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise ExecuteError(f"network error contacting {url}: {exc}") from exc

    def login(self, email: str, password: str) -> None:
        if not email or not password:
            raise ExecuteError("FPL_EMAIL / FPL_PASSWORD not set in the environment")
        form = urllib.parse.urlencode({
            "login": email,
            "password": password,
            "app": "plfpl-web",
            "redirect_uri": "https://fantasy.premierleague.com/a/login",
        }).encode()
        status, body = self._request(
            LOGIN_URL, data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        names = {c.name for c in self._jar}
        # A good login lands us with a pl_profile cookie; a bad one bounces back
        # to the login page with ?state=fail, and a device challenge returns an
        # interstitial with neither.
        if "pl_profile" not in names:
            if b"state=fail" in body or b"incorrect" in body.lower():
                raise ExecuteError("FPL login rejected — wrong email/password")
            raise ExecuteError(
                "FPL login did not establish a session (no pl_profile cookie). "
                "Most likely a new-device email verification or a CAPTCHA on the "
                "login page — the script cannot pass either. "
                "Use session-cookie mode or complete this GW manually."
            )

    def _api_get(self, path: str) -> dict:
        status, body = self._request(f"{API}{path}", headers={"X-Requested-With": "XMLHttpRequest"})
        if status == 403:
            raise ExecuteError(f"GET {path} -> 403 (not authenticated / session expired)")
        if status >= 400:
            raise ExecuteError(f"GET {path} -> {status}: {body[:300]!r}")
        return json.loads(body)

    def _api_post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        status, body = self._request(
            f"{API}{path}", data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://fantasy.premierleague.com/transfers",
            },
        )
        if status not in (200, 201, 204):
            raise ExecuteError(f"POST {path} -> {status}: {body[:400]!r}")
        return json.loads(body) if body else {}

    # ---- reads -------------------------------------------------------------- #

    def me(self) -> dict:
        """Authenticated account summary. `player.entry` is the numeric team id."""
        return self._api_get("/me/")

    def my_team(self, entry_id: int) -> dict:
        return self._api_get(f"/my-team/{entry_id}/")

    # ---- writes ---------------------------------------------------------- #

    def submit_transfers(self, payload: dict) -> dict:
        return self._api_post("/transfers/", payload)

    def submit_lineup(self, entry_id: int, payload: dict) -> dict:
        return self._api_post(f"/my-team/{entry_id}/", payload)


# --------------------------------------------------------------------------- #
# Payload construction                                                        #
# --------------------------------------------------------------------------- #

def build_transfers_payload(entry_id: int, event: int, my_team: dict,
                            transfers_in: list[int], transfers_out: list[int],
                            chip: str | None) -> dict:
    """Match ins to outs by position and attach the FPL-legal buy/sell prices."""
    from . import fpl_api
    players = fpl_api.players_by_id()
    sell_by_el = {p["element"]: p["selling_price"] for p in my_team.get("picks", [])}

    def pos(el: int) -> int:
        return players[el]["element_type"]

    outs = list(transfers_out)
    pairs = []
    for el_in in transfers_in:
        match = next((o for o in outs if pos(o) == pos(el_in)), None)
        if match is None:
            raise ExecuteError(
                f"cannot pair transfer-in {el_in} (type {pos(el_in)}) with any "
                f"transfer-out of the same position"
            )
        outs.remove(match)
        pairs.append({
            "element_in": el_in,
            "purchase_price": players[el_in]["now_cost"],
            "element_out": match,
            "selling_price": sell_by_el.get(match, players[match]["now_cost"]),
        })
    return {
        "chips": CHIP_API_NAME.get(chip) if chip else None,
        "entry": entry_id,
        "event": event,
        "transfers": pairs,
    }


def build_lineup_payload(starting_xi: list[int], bench_order: list[int],
                         captain: int, vice: int, chip: str | None) -> dict:
    """positions 1–11 = XI, 12–15 = bench (12 is the reserve GK)."""
    from . import fpl_api
    players = fpl_api.players_by_id()
    gk_bench = [e for e in bench_order if players[e]["element_type"] == 1]
    out_bench = gk_bench + [e for e in bench_order if players[e]["element_type"] != 1]

    picks = []
    for i, el in enumerate(starting_xi, start=1):
        picks.append({"element": el, "position": i,
                      "is_captain": el == captain, "is_vice_captain": el == vice})
    for j, el in enumerate(out_bench, start=12):
        picks.append({"element": el, "position": j,
                      "is_captain": el == captain, "is_vice_captain": el == vice})
    payload = {"picks": picks}
    if chip:
        payload["chip"] = CHIP_API_NAME.get(chip)
    return payload


# --------------------------------------------------------------------------- #
# Orchestration + guardrails                                                  #
# --------------------------------------------------------------------------- #

def _guardrail(state, plan, review, cfg) -> tuple[bool, str]:
    ex = (cfg or {}).get("execute", {})
    if not getattr(state, "auto_execute", False):
        return False, "state.json auto_execute is false"
    if not ex.get("armed", False):
        return False, "config execute.armed is false"
    if ex.get("dry_run", True):
        return False, "config execute.dry_run is true"
    want = [v.lower() for v in ex.get("require_verdict", ["endorse"])]
    got = (review.verdict.lower() if review and review.ok else "unavailable")
    if got not in want:
        return False, f"Claude verdict '{got}' not in {want}"
    hits = plan.next_gw.hits if plan.next_gw else 0
    if hits * 4 > ex.get("max_auto_hit", 0):
        return False, f"{hits} hit(s) exceeds execute.max_auto_hit={ex.get('max_auto_hit', 0)}"
    return True, "all guardrails passed"


def run_execution(state, plan, captain, cfg, review, *, deadline_hours: float,
                  force_dry: bool = False) -> ExecResult:
    """Apply plan.next_gw. Returns an ExecResult; raises ExecuteError on hard failure."""
    ex = (cfg or {}).get("execute", {})
    live_ok, reason = _guardrail(state, plan, review, cfg)
    freeze = ex.get("freeze_minutes", 45)
    if live_ok and deadline_hours * 60 < freeze:
        live_ok, reason = False, f"inside the {freeze}-min pre-deadline freeze window"

    dry = force_dry or not live_ok
    res = ExecResult(dry_run=dry, submitted=False,
                     reason_forced_dry=(reason if dry else None))
    res.line(f"guardrail: {reason}")
    res.line(f"mode: {'DRY-RUN (nothing submitted)' if dry else 'LIVE SUBMIT'}")

    if state.entry_id is None:
        raise ExecuteError("state.json has no entry_id — create the FPL team first")

    nxt = plan.next_gw
    if nxt is None:
        raise ExecuteError("no next-GW plan to execute")

    sess = FplSession()
    sess.login(os.environ.get("FPL_EMAIL", ""), os.environ.get("FPL_PASSWORD", ""))
    res.line("login: session established")

    team = sess.my_team(state.entry_id)
    bank = team.get("transfers", {}).get("bank")
    res.line(f"fetched my-team: {len(team.get('picks', []))} picks, bank {bank}")

    if nxt.transfers_in:
        res.transfers_payload = build_transfers_payload(
            state.entry_id, nxt.event, team,
            nxt.transfers_in, nxt.transfers_out, nxt.chip,
        )
        res.line("transfers payload:\n" + json.dumps(res.transfers_payload, indent=2))
    else:
        res.line("transfers: none (roll)")

    vice = nxt.vice_captain if nxt.vice_captain != captain.element else _vice(nxt, captain.element)
    res.lineup_payload = build_lineup_payload(
        nxt.starting_xi, nxt.bench_order, captain.element, vice, nxt.chip,
    )
    res.line("lineup payload:\n" + json.dumps(res.lineup_payload, indent=2))

    if dry:
        res.line("DRY-RUN: no POST made.")
        return res

    if res.transfers_payload:
        sess.submit_transfers(res.transfers_payload)
        res.line("submitted transfers OK")
    sess.submit_lineup(state.entry_id, res.lineup_payload)
    res.line("submitted lineup OK")
    res.submitted = True
    return res


def _vice(nxt, captain_el: int) -> int:
    return next((e for e in nxt.starting_xi if e != captain_el), captain_el)
