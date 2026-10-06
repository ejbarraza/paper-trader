"""run.py CLI: strategies are opt-in only -- nothing runs unless named."""

import sys
from types import SimpleNamespace

import pytest

import run
from run import resolve_strategies


def _args(**kw):
    base = dict(strategies="", pie=None, capital=10_000,
                scanner_dir="/tmp/nope")
    base.update(kw)
    return SimpleNamespace(**base)


def test_strategies_default_is_empty(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py"])
    assert run.parse_args().strategies == ""


def test_no_strategies_is_an_error():
    with pytest.raises(SystemExit, match="no strategies selected"):
        resolve_strategies(_args(), ["AAPL"])


def test_pie_flag_alone_does_not_enable_pie():
    with pytest.raises(SystemExit, match="not in --strategies"):
        resolve_strategies(_args(pie="AAPL:100"), ["AAPL"])


def test_pie_in_strategies_needs_allocation():
    with pytest.raises(SystemExit, match="needs a --pie allocation"):
        resolve_strategies(_args(strategies="pie"), ["AAPL"])


def test_named_strategy_is_selected():
    strats = resolve_strategies(_args(strategies="momentum"), ["AAPL"])
    assert [s.name for s in strats] == ["momentum"]


def test_named_pie_is_selected(tmp_path):
    f = tmp_path / "mypie.json"
    f.write_text('{"allocations": {"AAPL": 100}}')
    strats = resolve_strategies(
        _args(strategies="pie", pie=str(f)), ["AAPL"])
    assert [s.name for s in strats] == ["mypie"]
