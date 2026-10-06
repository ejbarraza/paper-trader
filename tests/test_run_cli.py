"""run.py CLI: strategies are opt-in only -- nothing runs unless named."""

import json
from types import SimpleNamespace

import pytest

from run import (apply_book, build_parser, normalize_symbols, resolve_strategies,
                 split_specs)
from strategies import PieStrategy


def _args(**kw):
    base = dict(strategies="", pie=None, capital=10_000,
                scanner_dir="/tmp/nope")
    base.update(kw)
    return SimpleNamespace(**base)


def test_strategies_default_is_empty():
    args = build_parser().parse_args(["backtest"])
    assert args.strategies == ""


def test_no_strategies_is_an_error():
    with pytest.raises(SystemExit, match="no strategies selected"):
        resolve_strategies(_args(), ["AAPL"])


def test_pie_flag_alone_selects_the_pie():
    strats = resolve_strategies(_args(pie="AAPL:100"), ["AAPL"])
    assert len(strats) == 1 and isinstance(strats[0], PieStrategy)


def test_pie_needs_an_allocation():
    with pytest.raises(SystemExit, match="needs an allocation"):
        resolve_strategies(_args(strategies="pie"), ["AAPL"])


def test_inline_pie_file_spec():
    strats = resolve_strategies(
        _args(strategies="pie:pies/qqq.json"), ["QQQ"])
    assert len(strats) == 1 and isinstance(strats[0], PieStrategy)


def test_inline_pie_weights_spec():
    strats = resolve_strategies(
        _args(strategies="pie:AAPL:50,MSFT:50"), ["AAPL", "MSFT"])
    assert len(strats) == 1 and isinstance(strats[0], PieStrategy)
    assert strats[0].allocations == {"AAPL": 0.5, "MSFT": 0.5}


def test_named_strategy_is_selected():
    strats = resolve_strategies(_args(strategies="momentum"), ["AAPL"])
    assert [s.name for s in strats] == ["momentum"]


def test_unknown_strategy_suggests():
    with pytest.raises(SystemExit, match="did you mean 'momentum'"):
        resolve_strategies(_args(strategies="momentu"), ["AAPL"])


def test_split_specs_keeps_pie_commas():
    assert split_specs("pie:AAPL:30,MSFT:30,momentum") == [
        "pie:AAPL:30,MSFT:30", "momentum"]
    assert split_specs("momentum,meanrev") == ["momentum", "meanrev"]
    assert split_specs("") == []


def test_normalize_symbols():
    assert normalize_symbols("AAPL, MSFT") == ["AAPL", "MSFT"]
    assert normalize_symbols(["AAPL", "MSFT"]) == ["AAPL", "MSFT"]


def test_book_fills_defaults_cli_wins(tmp_path):
    book = {"strategies": "momentum", "capital": 5000}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(book))
    parser = build_parser()
    args = parser.parse_args(["backtest", "--book", str(f)])
    args = apply_book(args, parser)
    assert args.strategies == "momentum"
    assert args.capital == 5000
    assert args.db == "paper.db"  # untouched default


def test_book_rejects_unknown_key(tmp_path):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"nope": 1}))
    parser = build_parser()
    args = parser.parse_args(["backtest", "--book", str(f)])
    with pytest.raises(SystemExit, match="unknown key"):
        apply_book(args, parser)


def test_cli_overrides_book(tmp_path):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"capital": 5000}))
    parser = build_parser()
    args = parser.parse_args(
        ["backtest", "--book", str(f), "--capital", "9000"])
    args = apply_book(args, parser)
    assert args.capital == 9000
