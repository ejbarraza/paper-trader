#!/usr/bin/env python3
"""Operator dashboard: one view over the paper engine's ledger.

Read-only -- it never touches the engine, the feeds, or anything live.
Run it with ``python run.py --dashboard`` after (or during) a backtest.

    python dashboard.py --db paper.db --port 5000
"""

from __future__ import annotations

import argparse
import json

from ledger import Ledger
from strategies import STRATEGY_INFO

_STRAT_ASSET = {info["class"].name: ",".join(sorted(info["class"].asset_classes))
                for info in STRATEGY_INFO.values()}

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trader — dashboard</title>
<style>
:root{color-scheme:dark}
body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:22px;margin:0 0 4px} .sub{color:#8b949e;margin-bottom:20px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 18px;min-width:150px}
.card .k{font-size:12px;color:#8b949e}.card .v{font-size:22px;font-weight:600}
.up{color:#3fb950}.down{color:#f85149}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500}
canvas{width:100%;height:260px;background:#161b22;border:1px solid #30363d;border-radius:8px}
section{margin-bottom:28px}h2{font-size:16px;margin-bottom:8px}
.pill{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;background:#21262d}
</style></head><body>
<h1>Paper Trader <span class="pill">paper only — no live orders</span></h1>
<div class="sub">strategy loops · shared risk arbiter · simulated fills</div>
<div class="cards" id="cards"></div>
<section><h2>Equity curve</h2><canvas id="eq" width="900" height="260"></canvas></section>
<section><h2>Open positions</h2><table id="pos"><tr><th>Position</th><th>Qty</th><th>Mark</th><th>Value</th></tr></table></section>
<section><h2>Strategy P&amp;L</h2><table id="strat"><tr><th>Strategy</th><th>Asset class</th><th>Trades</th><th>Net P&amp;L</th></tr></table></section>
<section><h2>Recent trades</h2><table id="trades"><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Action</th><th>Qty</th><th>Price</th><th>Cash Δ</th><th>Note</th></tr></table></section>
<section><h2>Risk events <span style="color:#8b949e;font-weight:normal">(orders the arbiter blocked)</span></h2><table id="risk"><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Action</th><th>Reason</th></tr></table></section>
<script>
const fmt$=x=>'$'+Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
async function j(p){return (await fetch(p)).json()}
function row(t,cells){const tr=document.createElement('tr');cells.forEach(c=>{const td=document.createElement('td');td.innerHTML=c;tr.appendChild(td)});t.appendChild(tr)}
(async()=>{
 const eq=await j('/api/equity');
 const last=eq.length?eq[eq.length-1]:{equity:0,cash:0};
 const first=eq.length?eq[0].equity:last.equity;
 const ret=first?((last.equity/first-1)*100):0;
 const cls=ret>=0?'up':'down';
 document.getElementById('cards').innerHTML=
  `<div class="card"><div class="k">Equity</div><div class="v">${fmt$(last.equity)}</div></div>
   <div class="card"><div class="k">Cash</div><div class="v">${fmt$(last.cash)}</div></div>
   <div class="card"><div class="k">Return</div><div class="v ${cls}">${ret.toFixed(2)}%</div></div>
   <div class="card"><div class="k">Data points</div><div class="v">${eq.length}</div></div>`;
 // chart
 const cv=document.getElementById('eq'),cx=cv.getContext('2d');
 const vals=eq.map(p=>p.equity),mn=Math.min(...vals),mx=Math.max(...vals),rg=(mx-mn)||1;
 cx.strokeStyle='#58a6ff';cx.lineWidth=2;cx.beginPath();
 vals.forEach((v,i)=>{const x=i/(vals.length-1||1)*cv.width,y=cv.height-10-((v-mn)/rg)*(cv.height-20);i?cx.lineTo(x,y):cx.moveTo(x,y)});
 cx.stroke();
 const pos=await j('/api/positions');
 pos.forEach(p=>row(document.getElementById('pos'),[p.pkey,p.qty,fmt$(p.mark),fmt$(p.qty*p.mark)]));
 const st=await j('/api/strategies');
 st.forEach(s=>row(document.getElementById('strat'),[s.strategy,s.asset_class,s.n_trades,`<span class="${s.net>=0?'up':'down'}">${fmt$(s.net)}</span>`]));
 const tr=await j('/api/trades');
 tr.slice(0,50).forEach(t=>row(document.getElementById('trades'),[t.ts.slice(0,16),t.strategy,t.symbol,t.action,t.qty,fmt$(t.price),fmt$(t.cash_delta),(t.note||'').slice(0,60)]));
 const rk=await j('/api/risk');
 rk.slice(0,50).forEach(r=>row(document.getElementById('risk'),[r.ts.slice(0,16),r.strategy,r.symbol,r.action,`<span class="pill">${r.reason}</span>`]));
})();
</script></body></html>
"""


def create_app(db_path: str):
    from flask import Flask, jsonify
    app = Flask(__name__)
    led = Ledger(db_path)

    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/api/equity")
    def api_equity():
        return jsonify(led.equity_curve())

    @app.get("/api/positions")
    def api_positions():
        return jsonify(led.latest_positions())

    @app.get("/api/strategies")
    def api_strategies():
        rows = led.strategy_pnl()
        for r in rows:
            r["asset_class"] = _STRAT_ASSET.get(r["strategy"], "?")
        return jsonify(rows)

    @app.get("/api/trades")
    def api_trades():
        return jsonify(led.trades())

    @app.get("/api/risk")
    def api_risk():
        return jsonify(led.risk_events())

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="paper.db")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    create_app(args.db).run(port=args.port)


if __name__ == "__main__":
    main()
