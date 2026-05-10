"""One-shot repair: recompute SL/TP for currently-open trades whose stored
stop_loss_price / take_profit_price look rounded to 2 decimals.

Background: pre-commit 30ea0f6, ``compute_trade_targets`` returned
``round(stop_loss_price, 2)`` / ``round(take_profit_price, 2)``. For
sub-dollar tokens that collapsed SL/TP to ``0.00`` or wildly wrong levels.
The producer is now fixed for new trades; this script repairs the rows that
were persisted before the fix.

Default mode is dry-run. Pass ``--commit`` to actually write the UPDATEs.

Usage:
    python3 scripts/repair_open_trade_targets.py            # dry run
    python3 scripts/repair_open_trade_targets.py --commit   # write changes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from trading.db.store import get_db, get_read_db  # noqa: E402
from trading.risk.manager import compute_trade_targets  # noqa: E402


SELECT_SQL = """
    SELECT id, symbol, price, qty, total,
           stop_loss_price, take_profit_price,
           trailing_stop_activate, risk_reward_ratio
    FROM trades
    WHERE side = 'buy'
      AND closed_at IS NULL
      AND (status IS NULL OR status != 'closed')
      AND stop_loss_price IS NOT NULL
      AND take_profit_price IS NOT NULL
      AND price > 0
"""


def looks_rounded(sl: float, tp: float, price: float) -> bool:
    """SL/TP look like they were rounded to 2 decimals.

    Skip BTC/ETH and other large-cap tokens — at price >= $100, 2-decimal
    storage is genuinely correct, not a bug.
    """
    if price >= 100:
        return False
    return sl == round(sl, 2) and tp == round(tp, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="Actually write UPDATEs (default: dry-run)")
    args = ap.parse_args()

    with get_read_db() as conn:
        rows = [dict(r) for r in conn.execute(SELECT_SQL).fetchall()]

    print(f"Open trades with SL/TP set: {len(rows)}")

    candidates = [
        r for r in rows
        if looks_rounded(r["stop_loss_price"], r["take_profit_price"], r["price"])
    ]
    print(f"Candidates for repair (price < $100, SL/TP look rounded): {len(candidates)}")
    if not candidates:
        print("Nothing to repair.")
        return 0

    planned: list[tuple[int, dict, dict]] = []
    failures: list[tuple[int, str]] = []

    for r in candidates:
        try:
            order_value = r["total"] or (r["qty"] or 0) * (r["price"] or 0)
            targets = compute_trade_targets(
                symbol=r["symbol"],
                entry_price=float(r["price"]),
                order_value=float(order_value),
            )
        except Exception as e:
            failures.append((r["id"], str(e)))
            continue

        new = {
            "stop_loss_price": targets.stop_loss_price,
            "take_profit_price": targets.take_profit_price,
            "trailing_stop_activate": targets.trailing_stop_activate_price,
            "risk_reward_ratio": targets.risk_reward_ratio,
        }
        planned.append((r["id"], r, new))

    print()
    print(f"{'id':>6}  {'symbol':<14} {'entry':>14}  "
          f"{'old SL':>14} -> {'new SL':>14}   "
          f"{'old TP':>14} -> {'new TP':>14}")
    print("-" * 130)
    for tid, old, new in planned:
        print(f"{tid:>6}  {old['symbol']:<14} {old['price']:>14.8f}  "
              f"{old['stop_loss_price']:>14.8f} -> {new['stop_loss_price']:>14.8f}   "
              f"{old['take_profit_price']:>14.8f} -> {new['take_profit_price']:>14.8f}")

    if failures:
        print()
        print(f"Failures ({len(failures)}):")
        for tid, msg in failures:
            print(f"  trade {tid}: {msg}")

    if not args.commit:
        print()
        print(f"Dry-run: {len(planned)} row(s) would be updated. "
              f"Re-run with --commit to apply.")
        return 0

    print()
    print(f"Committing {len(planned)} update(s)...")
    with get_db() as conn:
        for tid, _old, new in planned:
            conn.execute(
                "UPDATE trades SET stop_loss_price=?, take_profit_price=?, "
                "trailing_stop_activate=?, risk_reward_ratio=? WHERE id=?",
                (new["stop_loss_price"], new["take_profit_price"],
                 new["trailing_stop_activate"], new["risk_reward_ratio"], tid),
            )
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
