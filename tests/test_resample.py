"""Tick resampler: bucketing correctness on synthetic ticks."""

import csv
from datetime import datetime, timedelta

from resample import resample_ticks


def _write_ticks(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "symbol", "bid", "ask", "bid_size", "ask_size",
                    "last"])
        base = datetime(2024, 1, 1, 12, 0, 0)
        for i, (bid, ask) in enumerate(rows):
            ts = (base + timedelta(seconds=i * 20)).isoformat()
            w.writerow([ts, "MKT", bid, ask, 100, 100, ""])


def test_buckets_and_ohlc(tmp_path):
    # 6 ticks, 20s apart -> two 60s buckets at 12:00 and 12:01
    _write_ticks(str(tmp_path / "t.csv"),
                [(0.10, 0.12), (0.11, 0.13), (0.09, 0.11),
                 (0.20, 0.22), (0.21, 0.23), (0.19, 0.21)])
    out = str(tmp_path / "b.csv")
    assert resample_ticks(str(tmp_path / "t.csv"), out, freq_s=60) == 2
    with open(out) as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["ts"] == "2024-01-01T12:00:00"
    assert float(rows[0]["open"]) == 0.11   # mid of first tick
    assert float(rows[0]["high"]) == 0.12
    assert float(rows[0]["low"]) == 0.10
    assert float(rows[0]["close"]) == 0.10  # mid of third tick
    assert float(rows[1]["open"]) == 0.21
    # average bid/ask preserved for spread-aware fills
    assert float(rows[0]["bid"]) == round((0.10 + 0.11 + 0.09) / 3, 6)


def test_skips_bad_rows(tmp_path):
    p = tmp_path / "t.csv"
    with open(p, "w", newline="") as f:
        f.write("ts,symbol,bid,ask,bid_size,ask_size,last\n")
        f.write("2024-01-01T12:00:00,MKT,0.10,0.12,1,1,\n")
        f.write("not-a-time,MKT,bad,bad,1,1,\n")
    out = str(tmp_path / "b.csv")
    assert resample_ticks(str(p), out, freq_s=60) == 1


def test_resampled_csv_feeds_engine(tmp_path):
    _write_ticks(str(tmp_path / "t.csv"), [(0.10, 0.12)] * 4)
    out = str(tmp_path / "b.csv")
    resample_ticks(str(tmp_path / "t.csv"), out, freq_s=60)
    from datetime import date
    from feeds import CsvFeed
    bars = CsvFeed(str(tmp_path)).history("b", date(2024, 1, 1),
                                          date(2024, 1, 2))
    assert bars and bars[0].bid is not None and bars[0].ask is not None
