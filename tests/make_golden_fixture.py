#!/usr/bin/env python3
"""Generate tests/fixtures/golden_reconcile_v0_provisional.db.

PROVISIONAL -- per the project plan this dataset is formally blessed only
AFTER the execution-cost model lands (separate workstream: it changes trade
attribution).  Until then it pins today's hand-computed expectations.

Canonical hand-computed run (strategy "vrp", capital $100,000.00):

  Day 1  buy 10 AAA @ $50.00            cash_delta  -$500.00
           cash = 100000.00 - 500.00 = 99,500.00
           positions: spot 10 @ 50 -> equity = 99500 + 500 = 100,000.00
  Day 2  sell_put 1 AAA K=55 @ $2.00     premium = 1*2.00*100 = $200.00
           fees $1.30 -> cash_delta = +$198.70
           cash = 99500.00 + 198.70 = 99,698.70
           positions: spot 10 @ 52.00 (520) ; short put mark 150.00/contract
           (engine convention: option marks are per-contract dollars,
           i.e. per-share x 100) -> -1 x 150.00 = -150
           equity = 99698.70 + 520 - 150 = 100,068.70
  Day 3  buy_put_close 1 @ $1.00        cost = 1*1.00*100 + 1.30 = $101.30
           cash_delta = -$101.30
           cash = 99698.70 - 101.30 = 99,597.40
           positions: spot 10 @ 53.00 (530)
           equity = 99597.40 + 530 = 100,127.40
  Day 4  sell 10 AAA @ $53.00           cash_delta = +$530.00
           cash = 99597.40 + 530.00 = 100,127.40 ; flat
           equity = 100,127.40

Net cash flow: -500.00 + 198.70 - 101.30 + 530.00 = +$127.40
Realized P&L (gross of fees, by ledger design):
  spot: 10 * (53 - 50) = $30.00 ; put: 1 * (2.00 - 1.00) * 100 = $100.00
  total = $130.00 (1 win + 1 win)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import Ledger

OUT = (Path(__file__).resolve().parent / "fixtures"
       / "golden_reconcile_v0_provisional.db")


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    led = Ledger(str(OUT))
    d1 = datetime(2024, 5, 1, 12)
    d2 = datetime(2024, 5, 2, 12)
    d3 = datetime(2024, 5, 3, 12)
    d4 = datetime(2024, 5, 4, 12)

    led.record_trade(d1, "vrp", "AAA", "buy", 10, 50.00, -500.00, "golden")
    led.record_equity(d1, 100_000.00, 99_500.00)
    led.snapshot_positions(d1, {"spot:AAA": (10, 50.00)})

    led.record_trade(d2, "vrp", "AAA", "sell_put", 1, 2.00, 198.70,
                     "golden K=55")
    led.record_equity(d2, 100_068.70, 99_698.70)
    led.snapshot_positions(d2, {"spot:AAA": (10, 52.00),
                                "put:AAA:55:2024-06-21": (-1, 150.00)})

    led.record_trade(d3, "vrp", "AAA", "buy_put_close", 1, 1.00, -101.30,
                     "golden")
    led.record_equity(d3, 100_127.40, 99_597.40)
    led.snapshot_positions(d3, {"spot:AAA": (10, 53.00)})

    led.record_trade(d4, "vrp", "AAA", "sell", 10, 53.00, 530.00, "golden")
    led.record_equity(d4, 100_127.40, 100_127.40)
    led.snapshot_positions(d4, {})
    led.close()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
