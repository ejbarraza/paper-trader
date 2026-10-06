"""Market discovery: the filter is pure and unit-testable (no network)."""

from discover import is_tradeable, summarize


def _market(**kw):
    m = {"slug": "s", "question": "q?", "enableOrderBook": True,
         "clobTokenIds": '["a","b"]', "volumeNum": 200_000,
         "liquidityNum": 50_000}
    m.update(kw)
    return m


def test_accepts_liquid_market():
    assert is_tradeable(_market(), 100_000, 10_000)


def test_rejects_low_volume():
    assert not is_tradeable(_market(volumeNum=50_000), 100_000, 10_000)


def test_rejects_low_liquidity():
    assert not is_tradeable(_market(liquidityNum=1_000), 100_000, 10_000)


def test_rejects_no_orderbook():
    assert not is_tradeable(_market(enableOrderBook=False), 100_000, 10_000)


def test_rejects_missing_tokens():
    assert not is_tradeable(_market(clobTokenIds="[]"), 100_000, 10_000)
    assert not is_tradeable(_market(clobTokenIds=None), 100_000, 10_000)


def test_rejects_missing_slug():
    assert not is_tradeable(_market(slug=""), 100_000, 10_000)


def test_summarize_fields():
    s = summarize(_market())
    assert s["slug"] == "s" and s["volume"] == 200_000
