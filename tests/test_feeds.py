"""Feeds: CSV round-trip, feed registry, and the WS book parser (synthetic)."""

import csv
from datetime import datetime

import pytest

from feeds import Bar, CsvFeed, PolymarketWSFeed, build_feed


def _write_csv(path, symbol, closes):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "symbol", "open", "high", "low", "close", "volume"])
        for i, c in enumerate(closes):
            w.writerow([f"2024-01-{i + 1:02d}T00:00:00", symbol,
                        c, c, c, c, 1000])


def test_bar_bid_ask_default_none():
    b = Bar(ts=datetime.now(), symbol="X", open=1, high=1, low=1, close=1)
    assert b.bid is None and b.ask is None


def test_csv_feed_roundtrip(tmp_path):
    _write_csv(str(tmp_path / "AAA.csv"), "AAA", [10.0, 11.0, 12.0])
    feed = CsvFeed(str(tmp_path))
    from datetime import date
    bars = feed.history("AAA", date(2024, 1, 1), date(2024, 12, 31))
    assert [b.close for b in bars] == [10.0, 11.0, 12.0]
    q = feed.latest_quote("AAA")
    assert q is not None and q.mid == pytest.approx(12.0)


def test_csv_feed_missing_symbol(tmp_path):
    feed = CsvFeed(str(tmp_path))
    from datetime import date
    assert feed.history("NOPE", date(2024, 1, 1), date(2024, 12, 31)) == []
    assert feed.latest_quote("NOPE") is None


def test_build_feed_kinds(tmp_path):
    for kind in ("yahoo", "polymarket", "polymarket_us", "polymarket_ws"):
        assert build_feed(kind).name == kind
    assert build_feed("csv", directory=str(tmp_path)).name == "csv"
    with pytest.raises(ValueError):
        build_feed("nope")


def _ws_feed_with_book():
    feed = PolymarketWSFeed()
    feed._ensure_started = lambda: None  # no network in unit tests
    feed._on_text(
        '{"asset_id":"tok1","timestamp":"1791247323431",'
        '"bids":[{"price":"0.15","size":"100"},{"price":"0.14","size":"200"}],'
        '"asks":[{"price":"0.16","size":"150"}]}')
    return feed


def test_ws_snapshot_parsing():
    feed = _ws_feed_with_book()
    top = feed._top("tok1")
    assert top["bid"] == pytest.approx(0.15)
    assert top["ask"] == pytest.approx(0.16)
    assert top["bid_size"] == pytest.approx(100.0)


def test_ws_delta_add_and_remove_level():
    feed = _ws_feed_with_book()
    feed._on_text(
        '{"event_type":"price_change","asset_id":"tok1","changes":['
        '{"price":"0.155","size":"50","side":"BUY"},'
        '{"price":"0.15","size":"0","side":"BUY"}]}')
    top = feed._top("tok1")
    assert top["bid"] == pytest.approx(0.155)  # new best, old level removed
    assert 0.15 not in feed._books["tok1"]["bids"]


def test_ws_last_trade():
    feed = _ws_feed_with_book()
    feed._on_text(
        '{"event_type":"last_trade_price","asset_id":"tok1",'
        '"price":"0.157","size":"10","side":"BUY"}')
    assert feed._books["tok1"]["last"] == pytest.approx(0.157)


def test_ws_latest_quote_from_book():
    feed = _ws_feed_with_book()
    feed._asset_of["MKT"] = "tok1"  # _subscribe_symbol becomes a no-op
    q = feed.latest_quote("MKT")
    assert q is not None
    assert (q.bid, q.ask) == pytest.approx((0.15, 0.16))


def test_ws_history_is_empty():
    feed = PolymarketWSFeed()
    from datetime import date
    assert feed.history("MKT", date(2024, 1, 1), date(2024, 1, 2)) == []
