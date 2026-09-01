"""Log in and print the numeric FPL team id (entry_id) for state.json.

    FPL_EMAIL=... FPL_PASSWORD=... python scripts/whoami.py

The bot's `entry_id` is the integer in `fantasy.premierleague.com/entry/<N>/...`,
not the account GUID. This fetches it from the authenticated /api/me/ endpoint so
you don't have to hunt for it in a URL. Run via the `fpl-execute-dryrun`
workflow if the credentials only exist as GitHub secrets.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplbot.execute import ExecuteError, FplSession  # noqa: E402


def main() -> int:
    sess = FplSession()
    try:
        sess.login(os.environ.get("FPL_EMAIL", ""), os.environ.get("FPL_PASSWORD", ""))
        me = sess.me()
    except ExecuteError as exc:
        print(f"FAILED: {exc}")
        return 1

    player = me.get("player", {}) or {}
    entry = player.get("entry")
    print(f"entry_id : {entry}")
    print(f"name     : {player.get('first_name', '')} {player.get('last_name', '')}".rstrip())
    print(f"region   : {player.get('region_name', '')}")
    if entry:
        print(f'\nPut this in state.json:  "entry_id": {entry}')
    else:
        print("\nNo `entry` on the account — have you created a team for this season yet?")
        print("Full /api/me/ payload:\n" + json.dumps(me, indent=2)[:1500])
    return 0 if entry else 2


if __name__ == "__main__":
    raise SystemExit(main())
