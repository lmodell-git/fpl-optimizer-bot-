"""Score the prediction approach against actual gameweek points.

    python scripts/backtest.py                # all finished GWs
    python scripts/backtest.py --gws 1 2      # specific GWs
    python scripts/backtest.py --write        # also write data/backtest_latest.md

Directional while only a couple of gameweeks exist; re-run weekly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fplbot import backtest  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "backtest_latest.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gws", type=int, nargs="*", default=None)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    report = backtest.run(args.gws)
    print(report.text())
    if args.write:
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text("```\n" + report.text() + "\n```\n")
        print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
